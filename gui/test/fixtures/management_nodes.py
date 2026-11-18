"""Real ROS 2 nodes for the parameter-management and service-management tests.

Concepts:
    These fixtures are separate from ``fixture_nodes.py`` because the tests
    change them: parameters are set and services are called. Running them in
    their own process keeps the shared fixtures unchanged for other tests.

    The settings node ``/fixture_mgmt/settings`` declares one parameter per
    case: every settable type, an integer and a floating-point range, a
    read-only parameter, a parameter with dynamic typing, and a parameter
    whose set callback rejects the value ``forbidden`` with a reason.

    The node ``/fixture_mgmt/nonfinite`` declares doubles that JSON cannot
    express: ``nan_value`` (NaN), ``positive_infinity``, ``negative_infinity``,
    ``mixed_array`` ([1.5, NaN, inf, -inf]), and ``unbounded`` (0.5 with a
    floating-point range from -inf to +inf), plus the finite ``finite``
    (0.25) for comparison. It accepts every change.

    The services node ``/fixture_mgmt/services`` offers:
        /fixture/add_two_ints   example_interfaces/srv/AddTwoInts, answers at once
        /fixture/nested         rcl_interfaces/srv/SetParameters, nested messages and arrays,
                                answers with one echo result per parameter
        /fixture/slow           std_srvs/srv/Trigger, answers after three seconds
        /fixture/call_counts    std_srvs/srv/Trigger, reports how often each other service was called
    so tests can check that a confirmed call happened exactly once.

    ``--vanishing`` instead starts only ``/fixture_vanish/node`` with
    ``/fixture/vanish`` (std_srvs/srv/Trigger), which never answers; tests
    stop this process during a call to simulate a provider that disappears.

    Usage: ``python management_nodes.py [--vanishing]``; prints ``MANAGEMENT READY``.
"""
import json
import math
import os
import sys
import threading
import time

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rcl_interfaces.msg import FloatingPointRange, IntegerRange, ParameterDescriptor, SetParametersResult

from example_interfaces.srv import AddTwoInts
from rcl_interfaces.srv import SetParameters
from std_srvs.srv import Trigger


class Management:
    """Create the settings node and the services node and count service calls."""

    def __init__(self):
        """Declare every fixture parameter and create every fixture service.

        The call counter is shared by the service callbacks, which may run in
        parallel, so it is protected by a lock.
        """
        self.counts = {}
        self.lock = threading.Lock()
        self.settings = rclpy.create_node('settings', namespace='/fixture_mgmt')
        self.services = rclpy.create_node('services', namespace='/fixture_mgmt')
        declare = self.settings.declare_parameter
        declare('enabled', True)
        declare('label', 'alpha', ParameterDescriptor(description='A free text label'))
        declare('level', 4, ParameterDescriptor(description='Even level from 0 to 10',
                                                integer_range=[IntegerRange(from_value=0, to_value=10, step=2)]))
        declare('speed', 0.5, ParameterDescriptor(description='Speed in m/s',
                                                  floating_point_range=[FloatingPointRange(from_value=0.0, to_value=2.0, step=0.0)]))
        declare('gains', [1.0, 2.0])
        declare('names', ['a', 'b'])
        declare('counts', [1, 2, 3])
        declare('frozen', 7, ParameterDescriptor(description='Cannot be changed', read_only=True))
        declare('anything', 1, ParameterDescriptor(description='May change type', dynamic_typing=True))
        declare('guarded', 'ok', ParameterDescriptor(description='Rejects the value "forbidden"',
                                                     additional_constraints='Any text except "forbidden"'))
        self.settings.add_on_set_parameters_callback(self.guard)
        self.nonfinite = rclpy.create_node('nonfinite', namespace='/fixture_mgmt')
        unlimited = FloatingPointRange(from_value=-math.inf, to_value=math.inf, step=0.0)
        self.nonfinite.declare_parameter('nan_value', math.nan)
        self.nonfinite.declare_parameter('positive_infinity', math.inf)
        self.nonfinite.declare_parameter('negative_infinity', -math.inf)
        self.nonfinite.declare_parameter('mixed_array', [1.5, math.nan, math.inf, -math.inf])
        self.nonfinite.declare_parameter('unbounded', 0.5, ParameterDescriptor(
            description='Any double', floating_point_range=[unlimited]))
        self.nonfinite.declare_parameter('finite', 0.25)
        group = ReentrantCallbackGroup()
        self.services.create_service(AddTwoInts, '/fixture/add_two_ints', self.add, callback_group=group)
        self.services.create_service(SetParameters, '/fixture/nested', self.nested, callback_group=group)
        self.services.create_service(Trigger, '/fixture/slow', self.slow, callback_group=group)
        self.services.create_service(Trigger, '/fixture/call_counts', self.report, callback_group=group)

    def count(self, name):
        """Note one more call of the named service."""
        with self.lock:
            self.counts[name] = self.counts.get(name, 0) + 1

    def guard(self, parameters):
        """Reject setting ``guarded`` to ``forbidden``, with a reason; accept everything else."""
        for parameter in parameters:
            if parameter.name == 'guarded' and parameter.value == 'forbidden':
                return SetParametersResult(successful=False, reason='forbidden is not allowed here')
        return SetParametersResult(successful=True)

    def add(self, request, response):
        """Answer the sum of the two integers and count the call."""
        self.count('add_two_ints')
        response.sum = request.a + request.b
        return response

    def nested(self, request, response):
        """Answer one successful result per parameter, echoing its name, type number and integer array length."""
        self.count('nested')
        response.results = [SetParametersResult(
            successful=True, reason=f'echo:{item.name}:{item.value.type}:{len(item.value.integer_array_value)}')
            for item in request.parameters]
        return response

    def slow(self, request, response):
        """Wait three seconds, then answer success; used to test bounded waiting."""
        self.count('slow')
        time.sleep(3.0)
        response.success, response.message = True, 'slow answer'
        return response

    def report(self, request, response):
        """Answer the call counts of the other services as JSON text."""
        with self.lock:
            response.success, response.message = True, json.dumps(self.counts, sort_keys=True)
        return response


def vanishing():
    """Create the node whose service never answers, for the disappearing-provider test."""
    node = rclpy.create_node('node', namespace='/fixture_vanish')

    def never(request, response):
        """Block for as long as the process lives, so no answer is ever sent."""
        while True:
            time.sleep(0.1)

    node.create_service(Trigger, '/fixture/vanish', never, callback_group=ReentrantCallbackGroup())
    return [node]


def main():
    """Run the chosen fixtures until terminated; print ``MANAGEMENT READY`` once they exist."""
    rclpy.init()
    if '--vanishing' in sys.argv:
        nodes = vanishing()
    else:
        fixtures = Management()
        nodes = [fixtures.settings, fixtures.services, fixtures.nonfinite]
    executor = MultiThreadedExecutor(num_threads=4)
    for node in nodes:
        executor.add_node(node)
    print('MANAGEMENT READY', flush=True)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    if '--vanishing' in sys.argv:
        # Leave the ROS graph properly, then end at once without answering the pending call.
        for node in nodes:
            node.destroy_node()
        os._exit(0)


if __name__ == '__main__':
    main()
