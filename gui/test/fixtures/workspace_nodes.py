"""A real ROS 2 node that the Workspace tests start and stop to watch interfaces appear and disappear.

Concepts:
    The node ``/fixture_workspace/dynamic`` offers one interface of every
    kind the Workspace shows, each with a distinctive name:
        topic      /fixture_ws/dynamic_topic     std_msgs/msg/String, published twice a second
        service    /fixture_ws/dynamic_service   std_srvs/srv/Trigger
        action     /fixture_ws/dynamic_action    example_interfaces/action/Fibonacci
        parameter  dynamic_threshold = 0.5 (double)
    Starting the process makes all four appear in the ROS graph; stopping it
    with Ctrl+C makes them disappear. On Ctrl+C the node leaves the graph
    properly before the process ends, so discovery notices quickly.

    Usage: ``python workspace_nodes.py``; prints ``WORKSPACE READY`` once the node exists.
"""
import os

import rclpy
from rclpy.action import ActionServer
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor

from example_interfaces.action import Fibonacci
from std_msgs.msg import String
from std_srvs.srv import Trigger


def create_dynamic_node():
    """Create the node with its topic, service, action and parameter and return it with the action server.

    The topic publishes a short text twice a second, so it has a publisher.
    The service answers successfully at once. The action server accepts a
    goal and succeeds immediately with an empty sequence; the tests never
    send one, it only has to exist. The parameter is declared with the value
    0.5, which the Workspace must show under this node.
    """
    node = rclpy.create_node('dynamic', namespace='/fixture_workspace')
    publisher = node.create_publisher(String, '/fixture_ws/dynamic_topic', 10)
    node.create_timer(0.5, lambda: publisher.publish(String(data='dynamic')))

    def trigger(request, response):
        """Answer the Trigger service successfully."""
        response.success = True
        response.message = 'dynamic'
        return response

    def execute(goal_handle):
        """Finish any goal at once with an empty sequence."""
        goal_handle.succeed()
        return Fibonacci.Result()

    node.create_service(Trigger, '/fixture_ws/dynamic_service', trigger)
    server = ActionServer(node, Fibonacci, '/fixture_ws/dynamic_action', execute)
    node.declare_parameter('dynamic_threshold', 0.5)
    return node, server


def main():
    """Run the dynamic node until interrupted, then leave the ROS graph and end at once.

    ``WORKSPACE READY`` is printed when the node and its interfaces exist,
    so the tests wait for that line instead of sleeping. After Ctrl+C the
    action server and the node are destroyed, which tells the other
    participants that they are gone, and the process exits immediately.
    """
    rclpy.init()
    node, server = create_dynamic_node()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    print('WORKSPACE READY', flush=True)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    server.destroy()
    node.destroy_node()
    os._exit(0)


if __name__ == '__main__':
    main()
