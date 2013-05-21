# Copyright 2012 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import gear
import json
import logging
import time
import threading
from uuid import uuid4

from zuul.model import Build


class GearmanCleanup(threading.Thread):
    """ A thread that checks to see if outstanding builds have
    completed without reporting back. """
    log = logging.getLogger("zuul.JenkinsCleanup")

    def __init__(self, gearman):
        threading.Thread.__init__(self)
        self.gearman = gearman
        self.wake_event = threading.Event()
        self._stopped = False

    def stop(self):
        self._stopped = True
        self.wake_event.set()

    def run(self):
        while True:
            self.wake_event.wait(300)
            if self._stopped:
                return
            try:
                self.gearman.lookForLostBuilds()
            except:
                self.log.exception("Exception checking builds:")


def getJobData(job):
    if not len(job.data):
        return {}
    d = job.data[-1]
    if not d:
        return {}
    return json.loads(d)


class ZuulGearmanClient(gear.Client):
    def __init__(self, zuul_gearman):
        super(ZuulGearmanClient, self).__init__()
        self.__zuul_gearman = zuul_gearman

    def handleWorkComplete(self, packet):
        job = super(ZuulGearmanClient, self).handleWorkComplete(packet)
        self.__zuul_gearman.onBuildCompleted(job)
        return job

    def handleWorkFail(self, packet):
        job = super(ZuulGearmanClient, self).handleWorkFail(packet)
        self.__zuul_gearman.onBuildCompleted(job)
        return job

    def handleWorkStatus(self, packet):
        job = super(ZuulGearmanClient, self).handleWorkStatus(packet)
        self.__zuul_gearman.onWorkStatus(job)
        return job

    def handleWorkData(self, packet):
        job = super(ZuulGearmanClient, self).handleWorkData(packet)
        self.__zuul_gearman.onWorkStatus(job)
        return job

    def handleDisconnect(self, job):
        job = super(ZuulGearmanClient, self).handleDisconnect(job)
        self.__zuul_gearman.onDisconnect(job)

    def handleStatusRes(self, packet):
        try:
            job = super(ZuulGearmanClient, self).handleStatusRes(packet)
        except gear.UnknownJobError:
            handle = packet.getArgument(0)
            for build in self.__zuul_gearman.builds:
                if build.__gearman_job.handle == handle:
                    self.__zuul_gearman.onUnknownJob(job)


class Gearman(object):
    log = logging.getLogger("zuul.Gearman")
    negative_function_cache_ttl = 5

    def __init__(self, config, sched):
        self.sched = sched
        self.builds = {}
        self.meta_jobs = {}  # A list of meta-jobs like stop or describe
        server = config.get('gearman', 'server')
        if config.has_option('gearman', 'port'):
            port = config.get('gearman', 'port')
        else:
            port = 4730

        self.gearman = ZuulGearmanClient(self)
        self.gearman.addServer(server, port)

        self.cleanup_thread = GearmanCleanup(self)
        self.cleanup_thread.start()
        self.function_cache = set()
        self.function_cache_time = 0

    def stop(self):
        self.log.debug("Stopping")
        self.cleanup_thread.stop()
        self.cleanup_thread.join()
        self.gearman.shutdown()
        self.log.debug("Stopped")

    def isJobRegistered(self, name):
        if self.function_cache_time:
            for connection in self.gearman.active_connections:
                if connection.connect_time > self.function_cache_time:
                    self.function_cache = set()
                    self.function_cache_time = 0
                    break
        if name in self.function_cache:
            self.log.debug("Function %s is registered" % name)
            return True
        if ((time.time() - self.function_cache_time) <
            self.negative_function_cache_ttl):
            self.log.debug("Function %s is not registered "
                           "(negative ttl in effect)" % name)
            return False
        self.function_cache_time = time.time()
        for connection in self.gearman.active_connections:
            try:
                req = gear.StatusAdminRequest()
                connection.sendAdminRequest(req)
                req.waitForResponse()
            except Exception:
                self.log.exception("Exception while checking functions")
                continue
            for line in req.response.split('\n'):
                parts = [x.strip() for x in line.split()]
                if not parts or parts[0] == '.':
                    continue
                self.function_cache.add(parts[0])
        if name in self.function_cache:
            self.log.debug("Function %s is registered" % name)
            return True
        self.log.debug("Function %s is not registered" % name)
        return False

    def launch(self, job, change, pipeline, dependent_changes=[]):
        self.log.info("Launch job %s for change %s with dependent changes %s" %
                      (job, change, dependent_changes))
        dependent_changes = dependent_changes[:]
        dependent_changes.reverse()
        uuid = str(uuid4().hex)
        params = dict(ZUUL_UUID=uuid,
                      ZUUL_PROJECT=change.project.name)
        params['ZUUL_PIPELINE'] = pipeline.name
        if hasattr(change, 'refspec'):
            changes_str = '^'.join(
                ['%s:%s:%s' % (c.project.name, c.branch, c.refspec)
                 for c in dependent_changes + [change]])
            params['ZUUL_BRANCH'] = change.branch
            params['ZUUL_CHANGES'] = changes_str
            params['ZUUL_REF'] = ('refs/zuul/%s/%s' %
                                  (change.branch,
                                   change.current_build_set.ref))
            params['ZUUL_COMMIT'] = change.current_build_set.commit

            zuul_changes = ' '.join(['%s,%s' % (c.number, c.patchset)
                                     for c in dependent_changes + [change]])
            params['ZUUL_CHANGE_IDS'] = zuul_changes
            params['ZUUL_CHANGE'] = str(change.number)
            params['ZUUL_PATCHSET'] = str(change.patchset)
        if hasattr(change, 'ref'):
            params['ZUUL_REFNAME'] = change.ref
            params['ZUUL_OLDREV'] = change.oldrev
            params['ZUUL_NEWREV'] = change.newrev
            params['ZUUL_SHORT_OLDREV'] = change.oldrev[:7]
            params['ZUUL_SHORT_NEWREV'] = change.newrev[:7]

            params['ZUUL_REF'] = change.ref
            params['ZUUL_COMMIT'] = change.newrev

        # This is what we should be heading toward for parameters:

        # required:
        # ZUUL_UUID
        # ZUUL_REF (/refs/zuul/..., /refs/tags/foo, master)
        # ZUUL_COMMIT

        # optional:
        # ZUUL_PROJECT
        # ZUUL_PIPELINE

        # optional (changes only):
        # ZUUL_BRANCH
        # ZUUL_CHANGE
        # ZUUL_CHANGE_IDS
        # ZUUL_PATCHSET

        # optional (ref updated only):
        # ZUUL_OLDREV
        # ZUUL_NEWREV
        # ZUUL_SHORT_NEWREV
        # ZUUL_SHORT_OLDREV

        if callable(job.parameter_function):
            job.parameter_function(change, params)
            self.log.debug("Custom parameter function used for job %s, "
                           "change: %s, params: %s" % (job, change, params))

        if 'ZUUL_NODE' in params:
            name = "build:%s:%s" % (job.name, params['ZUUL_NODE'])
        else:
            name = "build:%s" % job.name
        build = Build(job, uuid)

        gearman_job = gear.Job(name, json.dumps(params),
                               unique=uuid)
        build.__gearman_job = gearman_job
        self.builds[uuid] = build

        if not self.isJobRegistered(gearman_job.name):
            self.log.error("Job %s is not registered with Gearman" %
                           gearman_job)
            self.onBuildCompleted(gearman_job, 'LOST')
            return build

        try:
            self.gearman.submitJob(gearman_job)
        except Exception:
            self.log.exception("Unable to submit job to Gearman")
            self.onBuildCompleted(gearman_job, 'LOST')
            return build

        gearman_job.waitForHandle(30)
        if not gearman_job.handle:
            self.log.error("No job handle was received for %s after 30 seconds"
                           " marking as lost." %
                           gearman_job)
            self.onBuildCompleted(gearman_job, 'LOST')

        return build

    def cancel(self, build):
        self.log.info("Cancel build %s for job %s" % (build, build.job))

        if build.number:
            self.log.debug("Build %s has already started" % build)
            self.cancelRunningBuild(build)
            self.log.debug("Canceled running build %s" % build)
            return
        else:
            self.log.debug("Build %s has not started yet" % build)

        self.log.debug("Looking for build %s in queue" % build)
        if self.cancelJobInQueue(build):
            self.log.debug("Removed build %s from queue" % build)
            return

        self.log.debug("Still unable to find build %s to cancel" % build)
        if build.number:
            self.log.debug("Build %s has just started" % build)
            self.cancelRunningBuild(build)
            self.log.debug("Canceled just running build %s" % build)
        else:
            self.log.error("Build %s has not started but "
                           "was not found in queue" % build)

    def onBuildCompleted(self, job, result=None):
        if job.unique in self.meta_jobs:
            del self.meta_jobs[job.unique]
            return

        build = self.builds.get(job.unique)
        if build:
            if result is None:
                data = getJobData(job)
                result = data.get('result')
            self.log.info("Build %s complete, result %s" %
                          (job, result))
            build.result = result
            self.sched.onBuildCompleted(build)
            # The test suite expects the build to be removed from the
            # internal dict after it's added to the report queue.
            del self.builds[job.unique]
        else:
            if not job.name.startswith("stop:"):
                self.log.error("Unable to find build %s" % job.unique)

    def onWorkStatus(self, job):
        data = getJobData(job)
        self.log.info("Build %s update" % job)
        build = self.builds.get(job.unique)
        if build:
            self.log.debug("Found build %s" % build)
            if not build.number:
                self.log.info("Build %s started" % job)
                build.url = data.get('full_url')
                build.number = data.get('number')
                build.__gearman_master = data.get('master')
                self.sched.onBuildStarted(build)
            build.fraction_complete = job.fraction_complete
        else:
            self.log.error("Unable to find build %s" % job.unique)

    def onDisconnect(self, job):
        self.log.info("Gearman job %s lost due to disconnect" % job)
        self.onBuildCompleted(job, 'LOST')

    def onUnknownJob(self, job):
        self.log.info("Gearman job %s lost due to unknown handle" % job)
        self.onBuildCompleted(job, 'LOST')

    def cancelJobInQueue(self, build):
        job = build.__gearman_job

        req = gear.CancelJobAdminRequest(job.handle)
        job.connection.sendAdminRequest(req)
        req.waitForResponse()
        self.log.debug("Response to cancel build %s request: %s" %
                       (build, req.response.strip()))
        if req.response.startswith("OK"):
            try:
                del self.builds[job.unique]
            except:
                pass
            return True
        return False

    def cancelRunningBuild(self, build):
        stop_uuid = str(uuid4().hex)
        stop_job = gear.Job("stop:%s" % build.__gearman_master,
                            build.uuid,
                            unique=stop_uuid)
        self.meta_jobs[stop_uuid] = stop_job
        self.log.debug("Submitting stop job: %s", stop_job)
        self.gearman.submitJob(stop_job)
        return True

    def setBuildDescription(self, build, desc):
        try:
            name = "set_description:%s" % build.__gearman_master
        except AttributeError:
            # We haven't yet received the first data packet that tells
            # us where the job is running.
            return False

        if not self.isJobRegistered(name):
            return False

        desc_uuid = str(uuid4().hex)
        data = dict(unique_id=build.uuid,
                    html_description=desc)
        desc_job = gear.Job(name, json.dumps(data), unique=desc_uuid)
        self.meta_jobs[desc_uuid] = desc_job
        self.log.debug("Submitting describe job: %s", desc_job)
        self.gearman.submitJob(desc_job)
        return True

    def lookForLostBuilds(self):
        self.log.debug("Looking for lost builds")
        for build in self.builds.values():
            if build.result:
                # The build has finished, it will be removed
                continue
            job = build.__gearman_job
            if not job.handle:
                # The build hasn't been enqueued yet
                continue
            p = gear.Packet(gear.constants.REQ, gear.constants.GET_STATUS,
                            job.handle)
            job.connection.sendPacket(p)