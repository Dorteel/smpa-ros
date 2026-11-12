#!/usr/bin/env python3
"""Segment one frame from the existing camera topic and save inspection images.

The client subscribes once; it never creates an image publisher. The overlay
colors selected pixels red while leaving unselected pixels unchanged.
"""
import argparse
import math
from pathlib import Path
import sys

import rclpy
from rclpy.qos import qos_profile_sensor_data
from rclpy.task import Future
from rclpy.utilities import remove_ros_args
from sensor_msgs.msg import Image
from smpa_ros_interfaces.srv import SegmentImage
import yaml
from PIL import Image as PILImage

from smpa_ros.sense import image_to_pil


def save_masks(image, binary_mask, directory):
    """Save the binary mask and a red overlay aligned with the captured image.

    Decode the original image and the returned mono8 mask with the existing
    image utility. Reject an unexpected mask encoding or different dimensions
    before producing files. Blend red into selected pixels, retain the other
    pixels, and save binary_mask.png and overlay.png in the requested directory.
    These inspection files are replaced when the client reuses that directory.
    """
    if binary_mask.encoding != 'mono8':
        raise ValueError('The segmentation service must return a mono8 binary mask')
    original = image_to_pil(image)
    mask = image_to_pil(binary_mask).convert('L')
    if mask.size != original.size:
        raise ValueError('Returned mask dimensions do not match the original image')
    red = PILImage.new('RGB', original.size, (255, 0, 0))
    overlay = PILImage.composite(PILImage.blend(original, red, 0.45), original, mask)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    mask.save(directory / 'binary_mask.png')
    overlay.save(directory / 'overlay.png')


def main(args=None):
    """Capture one camera frame, request segmentation, and save its result.

    The prompt and output directory come from command-line arguments. The
    topic defaults to camera_topic in the repository's existing config.yaml.
    A positive timeout bounds each wait for service discovery, a camera
    frame, and the service response. ROS remappings remain available.
    On success save the mask and overlay; on a failed response or timeout
    raise an explanatory error. Always release the subscription, client,
    node, and ROS context, including when an intermediate operation fails.
    """
    repository = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((repository / 'config.yaml').read_text())
    parser = argparse.ArgumentParser(description='Segment one existing camera frame; publish nothing.')
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--topic', default=config['camera_topic'])
    parser.add_argument('--output-dir', type=Path, default=Path('segmentation_output'))
    parser.add_argument('--timeout', type=float, default=60.0)
    options = parser.parse_args(remove_ros_args(args=args)[1:])
    if not options.prompt.strip() or not options.topic.strip():
        parser.error('prompt and topic must not be empty')
    if not math.isfinite(options.timeout) or options.timeout <= 0:
        parser.error('timeout must be finite and positive')
    rclpy.init(args=args)
    node = client = subscription = None
    try:
        node = rclpy.create_node('segment_image_client')
        client = node.create_client(SegmentImage, '/segment_image')
        if not client.wait_for_service(timeout_sec=options.timeout):
            raise TimeoutError('/segment_image is unavailable')
        captured = Future()

        def receive(image):
            """Remember the first camera frame without replacing it with later frames.

            The subscription can deliver additional messages while ROS spins.
            Set the future only once so the service request and saved overlay
            always refer to the same captured image.
            """
            if not captured.done():
                captured.set_result(image)

        subscription = node.create_subscription(Image, options.topic, receive, qos_profile_sensor_data)
        rclpy.spin_until_future_complete(node, captured, timeout_sec=options.timeout)
        if not captured.done():
            raise TimeoutError(f'No image received on {options.topic}')
        node.destroy_subscription(subscription)
        subscription = None
        image = captured.result()
        result = client.call_async(SegmentImage.Request(image=image, prompt=options.prompt))
        rclpy.spin_until_future_complete(node, result, timeout_sec=options.timeout)
        if not result.done():
            raise TimeoutError('Segmentation request timed out')
        response = result.result()
        if not response.success:
            raise RuntimeError(response.message)
        save_masks(image, response.binary_mask, options.output_dir)
        print(f'Saved {options.output_dir / "binary_mask.png"} and {options.output_dir / "overlay.png"}')
    finally:
        if subscription is not None:
            node.destroy_subscription(subscription)
        if client is not None:
            node.destroy_client(client)
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main(sys.argv)
