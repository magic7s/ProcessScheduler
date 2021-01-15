""" ProcessScheduler, Copyright 2020 Thomas Paviot (tpaviot@gmail.com)

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
You should have received a copy of the GNU General Public License along with
this program. If not, see <http://www.gnu.org/licenses/>.
"""

from typing import List, Optional

from z3 import Bool, Int, And, If

from processscheduler.base import _NamedUIDObject, is_strict_positive_integer, is_positive_integer
from processscheduler.resource import _Resource, Worker, SelectWorkers

import processscheduler.context as ps_context

class Task(_NamedUIDObject):
    """ a Task object """
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.work_amount = 0
        self.priority = 1  # by default

        # required resources to perform the task
        self.required_resources = [] # type: List[_Resource]

        # z3 Int variables
        self.start = Int('%s_start' % name)
        self.end = Int('%s_end' % name)
        self.scheduled = Bool('t%s_scheduled' % name)
        self.duration = Int('%s_duration' % name)

        # these two flags are set to True is there is a constraint
        # that set a lower or upper bound (e.g. a Precedence)
        # this is useful to reduce the number of assertions in z3
        # indeed if the task is lower_bounded by a precedence or
        # a StartAt, then there's no need to assert task.start >= 0
        self.lower_bounded = False
        # idem for the upper bound: no need to assert task.end <= horizon
        self.upper_bounded = False

        # add this task to the current context
        if ps_context.main_context is None:
            raise AssertionError('No context available. First create a SchedlingProblem')
        ps_context.main_context.add_task(self)

    def add_required_resource(self, resource: _Resource) -> None:
        """
        Add a required resource to the current task.

        Args:
            resource: any of one of the _Resource derivatives class (Worker, SelectWorkers etc.)
        """
        if not isinstance(resource, _Resource):
            raise TypeError('you must pass a Resource instance')
        if resource in self.required_resources:
            raise ValueError('resource %s already defined as a required resource for task %s' % (resource.name,
                                                                                                 self.name))
        if isinstance(resource, SelectWorkers):
            # loop over each resource
            for worker in resource.list_of_workers:
                resource_maybe_busy_start = Int('%s_maybe_busy_%s_start' % (worker.name, self.name))
                resource_maybe_busy_end = Int('%s_maybe_busy_%s_end' % (worker.name, self.name))
                # create the busy interval for the resource
                worker.add_busy_interval(self, (resource_maybe_busy_start, resource_maybe_busy_end))
                # add assertions. If worker is selected then sync the resource with the task
                selected_variable = resource.selection_dict[worker]
                schedule_as_usual = And(resource_maybe_busy_start ==  self.start,
                                        resource_maybe_busy_end ==  self.end)
                # in the case the worker is selected
                # else: reject in the past !! (i.e. this resource will be scheduled in the past)
                # to a place where they cannot conflict with the schedule
                # and with a zero busy duration, that mean they don't contribute in cost
                # or work amount
                move_to_past = And(resource_maybe_busy_start <= -1, # to past
                                   resource_maybe_busy_end == resource_maybe_busy_start) # zero dur.
                # define the assertion ...
                assertion = If(selected_variable, schedule_as_usual, move_to_past)
                # ... and store it into the task assertions list
                self.add_assertion(assertion)
                # also, don't forget to add the AlternativeWorker assertion
                self.add_assertion(resource.selection_assertion)
                # finally, add each worker to the "required" resource list
                self.required_resources.append(worker)
        elif isinstance(resource, Worker):
            resource_busy_start = Int('%s_busy_%s_start' % (resource.name, self.name))
            resource_busy_end = Int('%s_busy_%s_end' % (resource.name, self.name))
            # create the busy interval for the resource
            resource.add_busy_interval(self, (resource_busy_start, resource_busy_end))
            # set the busy resource to keep synced with the task
            self.add_assertion(resource_busy_start + self.duration == resource_busy_end)
            self.add_assertion(resource_busy_start ==  self.start)
            # finally, store this resource into the resource list
            self.required_resources.append(resource)

    def add_required_resources(self, list_of_resources: List[_Resource]) -> None:
        """
        Add a set of required resources to the current task.

        This method calls the add_required_resource method for each resource of the list.
        As a consequence, be aware this is not an atomic transaction.

        Args:
            list_of_resources: the list of resources to add.
        """
        for resource in list_of_resources:
            self.add_required_resource(resource)

class ZeroDurationTask(Task):
    """ Task with zero duration, an instant in the schedule.

    The task end and start are constrained to be equal.

    Args:
        name: the task name. It must be unique
    """
    def __init__(self, name: str) -> None:
        super().__init__(name)
        # add an assertion: end = start because the duration is zero
        self.add_assertion(self.start == self.end)
        self.add_assertion(self.duration == 0)

class FixedDurationTask(Task):
    """ Task with constant duration.

    Args:
        name: the task name. It must be unique
        duration: the task duration as a number of periods
        work_amount: represent the quantity of work this task must produce
        priority: the task priority. The greater the priority, the sooner it will be scheduled
    """
    def __init__(self, name: str,
                 duration: int,
                 work_amount: Optional[int] = 0,
                 priority: Optional[int] = 1) -> None:
        super().__init__(name)
        if not is_strict_positive_integer(duration):
            raise TypeError('duration must be a strict positive integer')
        if not is_positive_integer(work_amount):
            raise TypeError('work_amount me be a positive integer')
        self.work_amount = work_amount
        self.priority = priority
        # add an assertion: end = start + duration
        self.add_assertion(self.start + self.duration == self.end)
        self.add_assertion(self.duration == duration)

class UnavailabilityTask(FixedDurationTask):
    """ a task that tells that a resource is unavailable during this period. This
    task is not publicly exposed, it is used by the resource constraint ResourceUnavailability """
    def __init__(self, name: str, duration: int) -> None:
        super().__init__(name, duration)

class VariableDurationTask(Task):
    """ Tasj with a priori unknown duration. its duration is computed by the solver """
    def __init__(self, name: str,
                 length_at_least: Optional[int] = 0,
                 length_at_most: Optional[int] = None,
                 work_amount: Optional[int] = 0,
                 priority: Optional[int] = 1):
        super().__init__(name)

        if is_positive_integer(length_at_most):
            self.add_assertion(self.duration <= length_at_most)
        elif length_at_most is not None:
            raise TypeError('length_as_most should either be a positive integer or None')

        if not is_positive_integer(length_at_least):
            raise TypeError('length_at_least must be a positive integer')

        if not is_positive_integer(work_amount):
            raise TypeError('work_amount me be a positive integer')

        self.length_at_least = length_at_least
        self.length_at_most = length_at_most
        self.work_amount = work_amount
        self.priority = priority
        # set minimal duration
        self.add_assertion(self.duration >= length_at_least)
        # add an assertion: end = start + duration
        self.add_assertion(self.start + self.duration == self.end)
