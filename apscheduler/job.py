"""
Jobs represent scheduled tasks.
"""

from threading import Lock
from datetime import timedelta
from uuid import uuid4

import six

from apscheduler.util import ref_to_obj, obj_to_ref, get_callable_name, datetime_repr, repr_escape


class MaxInstancesReachedError(Exception):
    pass


class Job(object):
    """
    Encapsulates the actual Job along with its metadata. This class is used internally by APScheduler, and should never
    be instantiated by the user.
    """

    __slots__ = ('_lock', 'id', 'func', 'func_ref', 'trigger', 'args', 'kwargs', 'name', 'misfire_grace_time',
                 'coalesce', 'max_runs', 'max_instances', 'runs', 'instances', 'next_run_time')

    def __init__(self, trigger, func, args, kwargs, id, misfire_grace_time, coalesce, name,
                 max_runs, max_instances):
        if isinstance(func, six.string_types):
            self.func = ref_to_obj(func)
            self.func_ref = func
        elif callable(func):
            self.func = func
            try:
                self.func_ref = obj_to_ref(func)
            except ValueError:
                # If this happens, this Job won't be serializable
                self.func_ref = None
        else:
            raise TypeError('func must be a callable or a textual reference to one')

        self._lock = Lock()
        self.id = id or uuid4().hex
        self.trigger = trigger
        self.args = args
        self.kwargs = kwargs
        self.name = six.u(name or get_callable_name(self.func))
        self.misfire_grace_time = misfire_grace_time
        self.coalesce = coalesce
        self.max_runs = max_runs
        self.max_instances = max_instances
        self.runs = 0
        self.instances = 0
        self.next_run_time = None

    def compute_next_run_time(self, now):
        if self.runs == self.max_runs:
            self.next_run_time = None
        else:
            self.next_run_time = self.trigger.get_next_fire_time(now)

        return self.next_run_time

    def get_run_times(self, now):
        """
        Computes the scheduled run times between ``next_run_time`` and ``now``.
        """
        run_times = []
        run_time = self.next_run_time
        increment = timedelta(microseconds=1)
        while (not self.max_runs or self.runs < self.max_runs) and run_time and run_time <= now:
            run_times.append(run_time)
            run_time = self.trigger.get_next_fire_time(run_time + increment)

        return run_times

    def add_instance(self):
        with self._lock:
            if self.instances == self.max_instances:
                raise MaxInstancesReachedError
            self.instances += 1

    def remove_instance(self):
        with self._lock:
            assert self.instances > 0, 'Already at 0 instances'
            self.instances -= 1

    def __getstate__(self):
        if not self.func_ref:
            raise ValueError('This Job cannot be serialized because the function reference is missing')

        return {
            'version': 1,
            'id': self.id,
            'func_ref': self.func_ref,
            'trigger': self.trigger,
            'args': self.args,
            'kwargs': self.kwargs,
            'name': self.name,
            'misfire_grace_time': self.misfire_grace_time,
            'coalesce': self.coalesce,
            'max_runs': self.max_runs,
            'max_instances': self.max_instances,
            'runs': self.runs,
            'next_run_time': self.next_run_time,
        }

    def __setstate__(self, state):
        if state.get('version', 1) > 1:
            raise ValueError('Job has version %s, but only version 1 and lower can be handled' % state['version'])

        self.id = state['id']
        self.func_ref = state['func_ref']
        self.trigger = state['trigger']
        self.args = state['args']
        self.kwargs = state['kwargs']
        self.name = state['name']
        self.misfire_grace_time = state['misfire_grace_time']
        self.coalesce = state['coalesce']
        self.max_runs = state['max_runs']
        self.max_instances = state['max_instances']
        self.runs = state['runs']
        self.next_run_time = state['next_run_time']

        self._lock = Lock()
        self.func = ref_to_obj(self.func_ref)
        self.instances = 0

    def __eq__(self, other):
        if isinstance(other, Job):
            return self.id == other.id
        return NotImplemented

    def __repr__(self):
        return '<Job (id=%s)>' % repr_escape(self.id)


class JobHandle(object):
    __slots__ = ('_readonly', 'scheduler', 'jobstore', 'id', 'func_ref', 'trigger', 'args', 'kwargs', 'name',
                 'misfire_grace_time', 'coalesce', 'max_runs', 'max_instances', 'runs', 'instances', 'next_run_time')

    def __init__(self, scheduler, jobstore, job):
        super(JobHandle, self).__init__()
        self._readonly = False
        self.scheduler = scheduler
        self.jobstore = jobstore
        self.id = job.id
        self._update_attributes_from_job(job)
        self._readonly = True

    def remove(self):
        self.scheduler.unschedule_job(self.id, self.jobstore)

    def modify(self, **changes):
        self.scheduler.modify_job(self.id, self.jobstore, **changes)
        self._readonly = False
        try:
            self.id = changes.get('id', self.id)
            self.refresh()
        finally:
            self._readonly = True

    def refresh(self):
        job = self.scheduler.get_job(self.id, self.jobstore)
        self._readonly = False
        try:
            self._update_attributes_from_job(job)
        finally:
            self._readonly = True

    @property
    def pending(self):
        for job in self.scheduler.get_jobs(self.jobstore, pending=True):
            if job.id == self.id:
                return True
        return False

    def _update_attributes_from_job(self, job):
        self.func_ref = job.func_ref
        self.trigger = job.trigger
        self.args = job.args
        self.kwargs = job.kwargs
        self.name = job.name
        self.misfire_grace_time = job.misfire_grace_time
        self.coalesce = job.coalesce
        self.max_runs = job.max_runs
        self.max_instances = job.max_instances
        self.runs = job.runs
        self.next_run_time = job.next_run_time

    def __setattr__(self, key, value):
        if key == '_readonly' or not self._readonly:
            super(JobHandle, self).__setattr__(key, value)
        else:
            raise AttributeError('Cannot set job attributes directly. If you want to modify the job, use the modify() '
                                 'method instead.')

    def __eq__(self, other):
        if isinstance(other, JobHandle):
            return self.id == other.id
        return NotImplemented

    def __repr__(self):
        return '<JobHandle (id=%s name=%s)>' % (repr_escape(self.id), repr_escape(self.name))

    def __str__(self):
        return '%s (trigger: %s, next run at: %s)' % (self.name, repr_escape(str(self.trigger)),
                                                      datetime_repr(self.next_run_time))

    def __unicode__(self):
        return six.u('%s (trigger: %s, next run at: %s)') % (self.name, unicode(self.trigger),
                                                             datetime_repr(self.next_run_time))
