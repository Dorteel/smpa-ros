"""Exercise the independent Sense callback without generated type support."""
import importlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rclpy.action import GoalResponse
from smpa_ros_interfaces import action
from memory import MemoryManager


@pytest.fixture
def sense_module(monkeypatch):
    """Import the real Sense callback using a temporary action result stand-in.

    No ROS server, camera, microphone or model is started. Restore the module
    after each test so the fake generated action cannot affect other tests.
    """
    class Result:
        """Carry the observed text and persisted Sense record path."""
        def __init__(self):
            """Start with empty fields until sensing succeeds."""
            self.observation = ''
            self.record_path = ''
    monkeypatch.setattr(action, 'Sense', SimpleNamespace(Result=Result), raising=False)
    sys.modules.pop('smpa_ros.sense_node', None)
    module = importlib.import_module('smpa_ros.sense_node')
    yield module
    sys.modules.pop('smpa_ros.sense_node', None)


def test_sense_persists_observation_and_run_provenance(sense_module, monkeypatch, tmp_path):
    """Persist a mocked sensory observation using the real stage record writer.

    The callback uses an existing active run and frozen prompt. Confirm the
    observation, run-specific record and selected prompt are retained without
    invoking another cognitive stage or loading a model.
    """
    pipeline = {'sensing': {'vision': False}, 'stage_prompts': {'sense': {'name': 'chosen', 'text': 'Look'}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    memory.begin_cycle()
    def observe_once(node, config, directory, executor, timeout, service, speech, *, capture):
        """Return fixed sensory content while retaining the callback contract."""
        return '{"vision":"cup"}', ''
    monkeypatch.setattr(sense_module, 'observe', observe_once)
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=1.0 if name == 'timeout' else str(tmp_path)),
                           wait=Mock(), describe=Mock(), get_logger=lambda: Mock())
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id']), succeed=Mock(), abort=Mock())
    result = sense_module.SenseNode.execute(node, handle)
    handle.succeed.assert_called_once()
    record = json.loads((memory.directory / result.record_path).read_text())
    assert result.observation == '{"vision":"cup"}'
    assert record['output'] == result.observation and record['status'] == 'generated'
    assert record['prompt_name'] == 'chosen'


def test_sense_reports_failure_and_rejects_unknown_capability(sense_module, monkeypatch, tmp_path):
    """Persist sensor failures and reject unrelated capability selections.

    The fake sensor fails before producing an observation. The action must abort
    and retain a failure record; empty and explicit Sense capabilities remain
    accepted while an unsupported name is rejected before execution.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    memory.begin_cycle()
    def fail(*args, **kwargs):
        """Simulate an unavailable sensor without starting real hardware."""
        raise RuntimeError('sensor unavailable')
    monkeypatch.setattr(sense_module, 'observe', fail)
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value='sense' if name == 'default_capability' else 1.0 if name == 'timeout' else str(tmp_path)),
                           wait=Mock(), describe=Mock(), get_logger=lambda: Mock())
    assert sense_module.SenseNode.accept_goal(node, SimpleNamespace(capability='')) == GoalResponse.ACCEPT
    assert sense_module.SenseNode.accept_goal(node, SimpleNamespace(capability='sense')) == GoalResponse.ACCEPT
    assert sense_module.SenseNode.accept_goal(node, SimpleNamespace(capability='plan')) == GoalResponse.REJECT
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id']), succeed=Mock(), abort=Mock())
    sense_module.SenseNode.execute(node, handle)
    handle.abort.assert_called_once()
    record = json.loads((memory.directory / 'state/cycle_000001/sense.json').read_text())
    assert record['status'] == 'failed' and 'sensor unavailable' in record['error']


def test_camera_capture_uses_injected_description(assigned_test_run):
    """Persist camera evidence while an injected callable describes the image.

    Publish a single-pixel ROS camera frame, provide a mocked shared-inference
    callable, and verify that its output and original pixels are persisted. No
    legacy Sense service or real model is started.
    """
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image
    from PIL import Image as PILImage
    from smpa_ros.sense import capture_once

    rclpy.init(args=[])
    executor = SingleThreadedExecutor()
    camera = rclpy.create_node('mock_sense_camera')
    node = rclpy.create_node('mock_sense_capture')
    executor.add_node(camera)
    executor.add_node(node)
    topic = '/test/smpa_injected_camera'
    publisher = camera.create_publisher(Image, topic, qos_profile_sensor_data)
    image = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
    def publish():
        """Publish the same single camera frame until capture receives it."""
        publisher.publish(image)
    camera.create_timer(0.02, publish)
    def describe(frame, prompt):
        """Return a fixed description and check the original camera frame."""
        assert bytes(frame.data) == bytes([255, 0, 0])
        assert prompt == 'Describe the scene.'
        return 'red pixel'
    try:
        path = capture_once(node, {'camera_topic': topic, 'prompt': 'describe_scene'},
                            assigned_test_run.directory, executor, timeout=5, describe=describe)
        assert path.read_text() == 'red pixel'
        with PILImage.open(path.with_suffix('.png')) as saved:
            assert saved.getpixel((0, 0)) == (255, 0, 0)
            assert 'smpa.camera' in saved.info
    finally:
        executor.shutdown()
        camera.destroy_node()
        node.destroy_node()
        rclpy.shutdown()


def test_listen_failure_requests_cancellation(sense_module):
    """Cancel an accepted Listen goal when waiting for its result times out.

    Supply a fake action client and future responses. The first result wait
    fails; cleanup must request cancellation and wait for the terminal response,
    while the original timeout still reaches the caller.
    """
    handle = SimpleNamespace(accepted=True, get_result_async=Mock(side_effect=['first', 'terminal']),
                             cancel_goal_async=Mock(return_value='cancel'))
    client = SimpleNamespace(wait_for_server=Mock(return_value=True),
                             send_goal_async=Mock(return_value='accepted'))
    operations = []
    def result(future, timeout, operation):
        """Return mocked responses, failing the first terminal result wait."""
        operations.append(operation)
        if future == 'accepted':
            return handle
        if future == 'first':
            raise TimeoutError('fixture timeout')
        return SimpleNamespace()
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=1.0), result=result,
                           get_logger=lambda: Mock())
    with pytest.raises(TimeoutError, match='fixture timeout'):
        sense_module.SenseNode.perform(node, client, SimpleNamespace())
    handle.cancel_goal_async.assert_called_once()
    assert operations[-2:] == ['Listen cancellation', 'terminal Listen result']
