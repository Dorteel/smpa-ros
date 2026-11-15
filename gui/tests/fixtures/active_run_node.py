"""Stand in for a running SMPA robot session on the ROS graph.

Concepts:
    A running SMPA robot run places its nodes under ``/run_<uuid>`` and, with
    Hear enabled, offers a Listen action whose standard status topic is
    ``/run_<uuid>/sense/hear/listen/_action/status``. This fixture creates a
    node in that namespace and publishes that status topic, alternating every
    two seconds between a goal executing (status 2, "listening") and finished
    (status 4), so the GUI's liveness and live-Hear display can be tested
    without SMPA, its models or a microphone.

    Usage: ``python tests/fixtures/active_run_node.py <run uuid>``
"""
import sys
import uuid

import rclpy
from action_msgs.msg import GoalInfo, GoalStatus, GoalStatusArray
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy


def main():
    """Publish the run-scoped Listen status until terminated; print a ready line first."""
    identity = sys.argv[1]
    namespace = '/run_' + identity.replace('-', '_')
    rclpy.init()
    node = rclpy.create_node('smpa_loop', namespace=namespace + '/sense/vision')
    status = node.create_publisher(GoalStatusArray, namespace + '/sense/hear/listen/_action/status', QoSProfile(
        depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    goal = GoalInfo()
    goal.goal_id.uuid = list(uuid.uuid4().bytes)
    ticks = [0]

    def publish():
        """Alternate between listening and finished every two seconds."""
        ticks[0] += 1
        goal_status = GoalStatus(goal_info=goal, status=2 if (ticks[0] // 4) % 2 == 0 else 4)
        status.publish(GoalStatusArray(status_list=[goal_status]))

    node.create_timer(0.5, publish)
    publish()
    print('ACTIVE RUN READY', flush=True)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
