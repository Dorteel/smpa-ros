
#!/usr/bin/env python3
"""Continuously publish a fixed image as a ROS 2 camera topic."""

import cv2
import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from sensor_msgs.msg import Image

# Configuration: edit these values directly.
IMAGE_PATH = "/home/dorteel/ros2_ws/src/smpa-ros/images/test.png"
TOPIC = "/camera/color/image_raw"
FPS = 1.0


class ImagePublisher(Node):
    def __init__(self):
        super().__init__("image_publisher")

        image = cv2.imread(IMAGE_PATH)
        if image is None:
            raise FileNotFoundError(IMAGE_PATH)

        # Convert once, since the image never changes.
        self.msg = CvBridge().cv2_to_imgmsg(image, encoding="bgr8")
        self.msg.header.frame_id = "camera_color_optical_frame"

        self.publisher = self.create_publisher(Image, TOPIC, 10)
        self.timer = self.create_timer(1.0 / FPS, self.publish_image)

    def publish_image(self):
        # Refresh the timestamp on each publication.
        self.msg.header.stamp = self.get_clock().now().to_msg()
        self.publisher.publish(self.msg)


def main():
    rclpy.init()
    node = ImagePublisher()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
