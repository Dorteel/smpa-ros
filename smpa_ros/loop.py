"""One camera-to-Sense capture, plus the existing text-cycle helper."""

import argparse
from datetime import datetime
import json
from importlib.resources import files
from pathlib import Path
from time import perf_counter

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.task import Future
from rclpy.utilities import remove_ros_args
from sensor_msgs.msg import Image
from smpa_ros_interfaces.srv import DescribeScene
import yaml

from smpa_ros.backends.base import Backend


def run_cycle(observation: str, *, sense: Backend, model: Backend,
              plan: Backend, act: Backend) -> dict[str, str]:
    """Pass each stage's output to the next and return all intermediate results.

    Backends are supplied independently, so callers control selection and reuse.
    Exceptions propagate to the caller; a failed stage stops the cycle.
    """
    results = {}
    value = observation
    for name, backend in [('sense', sense), ('model', model), ('plan', plan), ('act', act)]:
        component = files('smpa_ros').joinpath(name)
        template = component.joinpath('prompts/default.txt').read_text(encoding='utf-8')
        config = json.loads(component.joinpath('models/default.json').read_text(encoding='utf-8'))
        value = backend.generate(template.format(input=value), **config)
        results[name] = value
    return results


def capture_once(node: Node, config: dict, output_dir: Path,
                 executor: SingleThreadedExecutor, timeout: float = 120) -> Path:
    """Receive one frame, request its description, and write the exact response."""
    prompt = files('smpa_ros.sense').joinpath(
        'prompts/' + config['prompt'] + '.txt',
    ).read_text(encoding='utf-8').strip()
    client = node.create_client(DescribeScene, '/sense')
    subscription = None
    try:
        node.get_logger().info('Waiting for /sense service')
        if not client.wait_for_service(timeout_sec=timeout):
            raise TimeoutError(f'/sense is unavailable after {timeout:g}s')
        image_future = Future()

        def receive(image: Image) -> None:
            if not image_future.done():
                image_future.set_result(image)

        node.get_logger().info(f"Subscribing to camera topic: {config['camera_topic']}")
        subscription = node.create_subscription(
            Image, config['camera_topic'], receive, qos_profile_sensor_data,
        )
        node.get_logger().info('Waiting for first image')
        started = perf_counter()
        executor.spin_until_future_complete(image_future, timeout_sec=timeout)
        if not image_future.done():
            raise TimeoutError(f"No image received on {config['camera_topic']} after {timeout:g}s")
        node.get_logger().info(f'Image received after {perf_counter() - started:.2f}s')
        node.destroy_subscription(subscription)
        subscription = None
        started = perf_counter()
        response_future = client.call_async(
            DescribeScene.Request(image=image_future.result(), prompt=prompt),
        )
        node.get_logger().info('Request sent to /sense')
        executor.spin_until_future_complete(response_future, timeout_sec=timeout)
        if not response_future.done():
            raise TimeoutError(f'/sense response timed out after {timeout:g}s')
        response = response_future.result()
        node.get_logger().info(f'/sense response received after {perf_counter() - started:.2f}s')
        if response.error:
            raise RuntimeError(response.error)
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
        path = output_dir / f'{timestamp}_sense_output.md'
        # Exclusive creation prevents an existing observation from being overwritten.
        with path.open('x', encoding='utf-8', newline='') as output:
            output.write(response.description)
        node.get_logger().info(f'Markdown file saved: {path.resolve()}')
        return path
    except Exception as error:
        node.get_logger().error(f'Camera-to-Sense pipeline failed: {error}')
        raise
    finally:
        if subscription is not None:
            node.destroy_subscription(subscription)
        node.destroy_client(client)


def main(args=None) -> None:
    parser = argparse.ArgumentParser(description='Describe one camera frame and exit.')
    parser.add_argument('--config', type=Path, default=Path(
        get_package_share_directory('smpa_ros')) / 'config/pipeline.yaml')
    parser.add_argument('--output-dir', type=Path, default=None)
    parser.add_argument('--timeout', type=float, default=120)
    options = parser.parse_args(remove_ros_args(args=args)[1:])
    if options.timeout <= 0:
        parser.error('--timeout must be positive')
    config = yaml.safe_load(options.config.read_text(encoding='utf-8'))
    rclpy.init(args=args)
    node = Node('smpa_loop')
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    try:
        capture_once(node, config, options.output_dir or Path(config['world_model_dir']), executor, options.timeout)
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
