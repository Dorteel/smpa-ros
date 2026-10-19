"""Ask one configurable visual question about one camera frame, then exit."""
import argparse
import json
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
from rclpy.task import Future
from rclpy.utilities import remove_ros_args
from sensor_msgs.msg import Image
from smpa_ros_interfaces.srv import EvaluateVisualDecision
import yaml

from smpa_ros.visual_jev.service import DEFAULT_QUESTION, validate_request


def evaluate_once(node, executor, topic, question=DEFAULT_QUESTION, choices=None,
                  context='', timeout=120):
    choices = ['yes', 'no'] if choices is None else choices
    validate_request(question, choices)
    client = node.create_client(EvaluateVisualDecision, '/visual_jev/evaluate')
    subscription = None
    try:
        if not client.wait_for_service(timeout_sec=timeout):
            raise TimeoutError('/visual_jev/evaluate is unavailable')
        frame = Future()

        def receive(image):
            if not frame.done():
                frame.set_result(image)

        node.get_logger().info(f'Waiting for one image on {topic}')
        subscription = node.create_subscription(Image, topic, receive, qos_profile_sensor_data)
        executor.spin_until_future_complete(frame, timeout_sec=timeout)
        if not frame.done():
            raise TimeoutError(f'No camera image received on {topic}')
        node.destroy_subscription(subscription)
        subscription = None
        future = client.call_async(EvaluateVisualDecision.Request(
            image=frame.result(), context=context, question=question, choices=choices,
        ))
        executor.spin_until_future_complete(future, timeout_sec=timeout)
        if not future.done():
            raise TimeoutError('Visual Jev response timed out')
        response = future.result()
        if not response.success:
            raise RuntimeError(response.error_message)
        if len(response.answer_labels) != len(response.probabilities):
            raise ValueError('Response label/probability length mismatch')
        return response
    finally:
        if subscription is not None:
            node.destroy_subscription(subscription)
        node.destroy_client(client)


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topic', help='Default: camera_topic in pipeline.yaml')
    parser.add_argument('--question', default=DEFAULT_QUESTION)
    parser.add_argument('--choices', nargs='+', default=['yes', 'no'])
    parser.add_argument('--context', default='')
    parser.add_argument('--timeout', type=float, default=120)
    options = parser.parse_args(remove_ros_args(args=args)[1:])
    if options.timeout <= 0:
        parser.error('--timeout must be positive')
    config = Path(get_package_share_directory('smpa_ros')) / 'config/pipeline.yaml'
    topic = options.topic or yaml.safe_load(config.read_text())['camera_topic']
    rclpy.init(args=args)
    node = rclpy.create_node('visual_jev_camera')
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    try:
        response = evaluate_once(node, executor, topic, options.question, options.choices,
                                 options.context, options.timeout)
        print(json.dumps({'selected_answer': response.selected_answer,
                          'probabilities': dict(zip(response.answer_labels, response.probabilities))},
                         indent=2))
    except Exception as error:
        node.get_logger().error(str(error))
        raise
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
