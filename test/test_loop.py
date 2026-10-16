from unittest.mock import MagicMock

import pytest
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from smpa_ros.loop import capture_once
from smpa_ros.sense.service import SenseService


@pytest.mark.parametrize('fails', [False, True])
def test_camera_sense_markdown(tmp_path, fails):
    """Only inference is mocked; camera messages and service calls use ROS."""
    rclpy.init(args=[])
    executor = SingleThreadedExecutor()
    camera = loop = server = None
    try:
        backend = MagicMock()
        text = '# Observation\n\nA red pixel.\n'
        backend.describe.return_value = text
        if fails:
            backend.describe.side_effect = RuntimeError('model failed')
        server = SenseService(backend=backend)
        camera = rclpy.create_node('test_camera')
        loop = rclpy.create_node('test_loop')
        for node in (server, camera, loop):
            executor.add_node(node)
        topic = '/test/smpa_camera'
        publisher = camera.create_publisher(Image, topic, qos_profile_sensor_data)
        image = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
        camera.create_timer(0.02, lambda: publisher.publish(image))
        config = {'camera_topic': topic, 'model': 'smolvlm2', 'prompt': 'describe_scene'}
        if fails:
            with pytest.raises(RuntimeError, match='model failed'):
                capture_once(loop, config, tmp_path, executor, timeout=5)
            assert not list(tmp_path.glob('*.md'))
        else:
            path = capture_once(loop, config, tmp_path, executor, timeout=5)
            assert path.name.endswith('_sense_output.md')
            assert path.read_bytes() == text.encode('utf-8')
            assert list(tmp_path.glob('*.md')) == [path]
        backend.describe.assert_called_once()
        assert backend.describe.call_args.args[1] == 'Describe the scene.'
    finally:
        executor.shutdown()
        for node in (loop, camera, server):
            if node is not None:
                node.destroy_node()
        rclpy.shutdown()
