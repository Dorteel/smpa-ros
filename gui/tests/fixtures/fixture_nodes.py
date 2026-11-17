"""Real ROS 2 nodes that give the GUI tests something known to discover.

Concepts:
    A fixture is a small, predictable ROS participant: a talker and listener
    pair, a node with declared parameters, a node that exists but never
    answers (it is not spun), two action servers with a client, and one
    publisher per supported sensor type.

    The Fibonacci action server behaves like a slow robot action: it sends
    feedback five times per second, can be cancelled, and has special orders
    that make it reject a goal, abort, or refuse cancellation. The lookup
    action server has a goal with nested messages and answers at once. All names start with ``fixture`` so
    tests can tell them from anything else on the ROS graph.
"""
import array
import io
import math
import struct
import sys

import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data

from example_interfaces.action import Fibonacci
from sensor_msgs.msg import (CameraInfo, CompressedImage, Image, Imu, JointState, LaserScan, NavSatFix,
                             PointCloud2, PointField, Range)
from std_msgs.msg import String
from tf2_msgs.action import LookupTransform

IMAGE_WIDTH, IMAGE_HEIGHT = 64, 48
CLOUD_POINTS = 5000


def test_image(counter):
    """Build an RGB image whose top-left pixel is pure red and which changes over time.

    A horizontal gradient fills the picture and a white column moves with the
    counter, so successive frames differ. The known red corner lets a test
    check that colour channels are decoded in the right order.
    """
    pixels = bytearray()
    column = counter % IMAGE_WIDTH
    for y in range(IMAGE_HEIGHT):
        for x in range(IMAGE_WIDTH):
            if x < 8 and y < 8:
                pixels += bytes((255, 0, 0))
            elif x == column:
                pixels += bytes((255, 255, 255))
            else:
                pixels += bytes((0, int(255 * x / IMAGE_WIDTH), int(255 * y / IMAGE_HEIGHT)))
    return bytes(pixels)


def test_cloud(node):
    """Build a point cloud of a unit sphere with a packed colour per point.

    Each point record holds x, y, z as 32-bit floats and an ``rgb`` field in
    the usual packed layout. One record is deliberately not-a-number so tests
    can check that invalid points are dropped.
    """
    cloud = PointCloud2()
    cloud.header.frame_id = 'fixture_lidar'
    cloud.header.stamp = node.get_clock().now().to_msg()
    cloud.height, cloud.width = 1, CLOUD_POINTS
    cloud.fields = [PointField(name=name, offset=offset, datatype=PointField.FLOAT32, count=1)
                    for name, offset in (('x', 0), ('y', 4), ('z', 8), ('rgb', 12))]
    cloud.point_step, cloud.row_step, cloud.is_dense = 16, 16 * CLOUD_POINTS, False
    data = bytearray()
    for index in range(CLOUD_POINTS):
        if index == 0:
            data += struct.pack('<fffI', math.nan, 0.0, 0.0, 0)
            continue
        polar = math.acos(1 - 2 * index / CLOUD_POINTS)
        around = index * 2.399963
        x, y, z = (math.sin(polar) * math.cos(around), math.sin(polar) * math.sin(around), math.cos(polar))
        colour = (int(255 * (x + 1) / 2) << 16) | (int(255 * (y + 1) / 2) << 8) | int(255 * (z + 1) / 2)
        data += struct.pack('<fffI', x, y, z, colour)
    cloud.data = array.array('B', bytes(data))
    return cloud


class Fixtures:
    """Create every fixture node and publish their data on timers."""

    def __init__(self):
        """Create the nodes, their publishers, parameters and the action pair.

        The silent node is created but never added to the executor, so its
        parameter services exist and never answer. Everything else is spun.
        """
        self.counter = 0
        self.talker = rclpy.create_node('fixture_talker')
        self.listener = rclpy.create_node('fixture_listener')
        self.parameters = rclpy.create_node('fixture_parameters', namespace='/fixture_ns')
        self.silent = rclpy.create_node('fixture_silent')
        self.sensors = rclpy.create_node('fixture_sensors')
        self.action_server_node = rclpy.create_node('fixture_action_server')
        self.action_client_node = rclpy.create_node('fixture_action_client')
        self.spun = [self.talker, self.listener, self.parameters, self.sensors,
                     self.action_server_node, self.action_client_node]

        self.chatter = self.talker.create_publisher(String, '/fixture/chatter', 10)
        self.listener.create_subscription(String, '/fixture/chatter', lambda message: None, 10)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.latched = self.talker.create_publisher(String, '/fixture/latched', latched)
        self.latched.publish(String(data='published once'))

        self.parameters.declare_parameter('fixture_integer', 42)
        self.parameters.declare_parameter('fixture_string', 'hello')
        self.parameters.declare_parameter('fixture_double', 1.5)
        self.parameters.declare_parameter('fixture_flags', [True, False])
        self.silent.declare_parameter('never_readable', 1)

        group = ReentrantCallbackGroup()
        self.action_server = ActionServer(
            self.action_server_node, Fibonacci, '/fixture/fibonacci', self.execute, callback_group=group,
            goal_callback=lambda goal: GoalResponse.REJECT if goal.order < 0 else GoalResponse.ACCEPT,
            cancel_callback=lambda handle: CancelResponse.REJECT if handle.request.order == 7 else CancelResponse.ACCEPT)
        self.lookup_server = ActionServer(self.action_server_node, LookupTransform, '/fixture/lookup_transform',
                                          self.lookup, callback_group=group)
        self.action_client = ActionClient(self.action_client_node, Fibonacci, '/fixture/fibonacci')

        create = self.sensors.create_publisher
        self.image = create(Image, '/fixture/camera/image_raw', qos_profile_sensor_data)
        self.depth = create(Image, '/fixture/camera/depth', qos_profile_sensor_data)
        self.odd = create(Image, '/fixture/camera/unsupported', qos_profile_sensor_data)
        self.compressed = create(CompressedImage, '/fixture/camera/image_raw/compressed', qos_profile_sensor_data)
        self.info = create(CameraInfo, '/fixture/camera/camera_info', 10)
        self.cloud = create(PointCloud2, '/fixture/points', qos_profile_sensor_data)
        self.scan = create(LaserScan, '/fixture/scan', qos_profile_sensor_data)
        self.imu = create(Imu, '/fixture/imu', qos_profile_sensor_data)
        self.joints = create(JointState, '/fixture/joint_states', 10)
        self.fix = create(NavSatFix, '/fixture/gps/fix', 10)
        self.range = create(Range, '/fixture/range', 10)
        self.talker.create_timer(0.1, self.publish_chatter)
        self.sensors.create_timer(0.1, self.publish_sensors)

    def execute(self, goal_handle):
        """Compute a Fibonacci sequence slowly, one number and one feedback message every 0.2 seconds.

        A negative order was already rejected before this runs. Order 13
        aborts after the first feedback. A cancel request ends the goal as
        cancelled with the numbers computed so far (order 7 refuses cancel
        requests and therefore always completes). Otherwise the goal
        succeeds with ``order + 1`` numbers.
        """
        import time
        sequence = [0, 1]
        for index in range(1, goal_handle.request.order):
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                return Fibonacci.Result(sequence=sequence)
            sequence.append(sequence[index] + sequence[index - 1])
            goal_handle.publish_feedback(Fibonacci.Feedback(sequence=sequence))
            if goal_handle.request.order == 13:
                goal_handle.abort()
                return Fibonacci.Result(sequence=sequence)
            time.sleep(0.2)
        goal_handle.succeed()
        return Fibonacci.Result(sequence=sequence[:goal_handle.request.order + 1])

    def lookup(self, goal_handle):
        """Answer a lookup goal at once, echoing goal values so tests can see they arrived intact."""
        goal = goal_handle.request
        result = LookupTransform.Result()
        result.transform.header.frame_id = goal.target_frame
        result.transform.child_frame_id = goal.source_frame
        result.transform.header.stamp = goal.source_time
        result.transform.transform.translation.x = float(goal.timeout.sec)
        result.transform.transform.translation.y = 1.0 if goal.advanced else 0.0
        goal_handle.succeed()
        return result

    def publish_chatter(self):
        """Publish a numbered text message ten times per second."""
        self.counter += 1
        self.chatter.publish(String(data=f'fixture message {self.counter}'))

    def stamped(self, message, frame):
        """Fill a message header with the current time and a frame name."""
        message.header.stamp = self.sensors.get_clock().now().to_msg()
        message.header.frame_id = frame
        return message

    def publish_sensors(self):
        """Publish one message of every supported sensor type with known contents.

        The values are chosen so tests can check them: a red image corner, a
        depth ramp, an image encoding the viewer does not support, a sphere
        point cloud, a square laser room with one infinite ray, a quarter
        turn about the vertical axis for the IMU, three named joints, a
        position in Amsterdam, a one metre range and a simple pinhole
        calibration.
        """
        pixels = test_image(self.counter)
        image = self.stamped(Image(), 'fixture_camera')
        image.width, image.height, image.step, image.encoding = IMAGE_WIDTH, IMAGE_HEIGHT, IMAGE_WIDTH * 3, 'rgb8'
        image.data = array.array('B', pixels)
        self.image.publish(image)

        depth = self.stamped(Image(), 'fixture_camera')
        depth.width, depth.height, depth.step, depth.encoding = 16, 8, 32, '16UC1'
        depth.data = array.array('B', struct.pack('<128H', *range(128)))
        self.depth.publish(depth)

        odd = self.stamped(Image(), 'fixture_camera')
        odd.width, odd.height, odd.step, odd.encoding = 4, 2, 8, 'bayer_rggb16'
        odd.data = array.array('B', bytes(16))
        self.odd.publish(odd)

        from PIL import Image as PILImage
        buffer = io.BytesIO()
        PILImage.frombytes('RGB', (IMAGE_WIDTH, IMAGE_HEIGHT), pixels).save(buffer, format='JPEG')
        compressed = self.stamped(CompressedImage(), 'fixture_camera')
        compressed.format = 'rgb8; jpeg compressed bgr8'
        compressed.data = array.array('B', buffer.getvalue())
        self.compressed.publish(compressed)

        info = self.stamped(CameraInfo(), 'fixture_camera')
        info.width, info.height, info.distortion_model = IMAGE_WIDTH, IMAGE_HEIGHT, 'plumb_bob'
        info.d = [0.1, -0.2, 0.0, 0.0, 0.0]
        info.k = [50.0, 0.0, 32.0, 0.0, 50.0, 24.0, 0.0, 0.0, 1.0]
        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        info.p = [50.0, 0.0, 32.0, 0.0, 0.0, 50.0, 24.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        self.info.publish(info)

        self.cloud.publish(test_cloud(self.sensors))

        scan = self.stamped(LaserScan(), 'fixture_laser')
        scan.angle_min, scan.angle_max, scan.angle_increment = -math.pi, math.pi, 2 * math.pi / 360
        scan.range_min, scan.range_max = 0.1, 10.0
        ranges = [2.0 / max(abs(math.cos(-math.pi + i * scan.angle_increment)),
                            abs(math.sin(-math.pi + i * scan.angle_increment))) for i in range(360)]
        ranges[0] = math.inf
        scan.ranges = ranges
        self.scan.publish(scan)

        imu = self.stamped(Imu(), 'fixture_imu')
        imu.orientation.z, imu.orientation.w = math.sin(math.pi / 4), math.cos(math.pi / 4)
        imu.angular_velocity.z = 0.5
        imu.linear_acceleration.z = 9.81
        self.imu.publish(imu)

        joints = self.stamped(JointState(), '')
        joints.name = ['fixture_joint_a', 'fixture_joint_b', 'fixture_joint_c']
        joints.position = [0.1 * math.sin(self.counter / 10), 0.5, -1.0]
        joints.velocity = [0.01, 0.0, 0.0]
        self.joints.publish(joints)

        fix = self.stamped(NavSatFix(), 'fixture_gps')
        fix.latitude, fix.longitude, fix.altitude = 52.3340, 4.8660, 5.0
        fix.status.status, fix.status.service = 0, 1
        fix.position_covariance = [4.0, 0.0, 0.0, 0.0, 4.0, 0.0, 0.0, 0.0, 9.0]
        fix.position_covariance_type = 2
        self.fix.publish(fix)

        reading = self.stamped(Range(), 'fixture_sonar')
        reading.radiation_type, reading.field_of_view = Range.ULTRASOUND, 0.5
        reading.min_range, reading.max_range, reading.range = 0.02, 4.0, 1.0
        self.range.publish(reading)


def main():
    """Run all fixtures until the process is terminated.

    Print ``FIXTURES READY`` once every node exists, so a test can wait for
    that line instead of sleeping for an arbitrary time.
    """
    rclpy.init()
    fixtures = Fixtures()
    executor = MultiThreadedExecutor(num_threads=8)
    for node in fixtures.spun:
        executor.add_node(node)
    print('FIXTURES READY', flush=True)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.flush()


if __name__ == '__main__':
    main()
