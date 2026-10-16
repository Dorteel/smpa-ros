import rclpy
from rclpy.node import Node


def main(args=None):
    rclpy.init(args=args)
    try:
        node = Node('hello')
        try:
            node.get_logger().info('Hello ROS 2!')
        finally:
            node.destroy_node()
    finally:
        rclpy.shutdown()
