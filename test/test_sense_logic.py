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


def test_refinement_image_is_frozen_for_shared_inference(monkeypatch, tmp_path):
    """Publish and reuse complete original pixels for refinement inference.

    Mock parameter responses and the structured image action. Two different
    camera frames must send the same first image to inference, with the run ID
    and schema attached. The persisted frozen PNG must contain the first frame.
    """
    from sensor_msgs.msg import Image
    from PIL import Image as PILImage
    from smpa_ros.sense import describe_image
    from smpa_ros_interfaces import action
    monkeypatch.setattr(action, 'DescribeStructuredImage', SimpleNamespace(Goal=SimpleNamespace))
    pipeline = {'enabled_actions': ['observe'], 'scene_refinement': {'enabled': True},
        'stage_settings': {'sense': {'temperature': 0.0, 'max_output_tokens': 32, 'reasoning_effort': None,
                                    'seed': None, 'response_format': 'json_schema', 'output_schema': {'type': 'object'}}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    calls = []
    def get_parameters(names):
        """Return one matching run and its frozen generation controls."""
        if names[0] == 'experiment_run_id':
            return SimpleNamespace(values=[SimpleNamespace(string_value=memory.metadata['run_id']),
                                           SimpleNamespace(string_value='fixture')])
        return SimpleNamespace(values=[SimpleNamespace(integer_value=32), SimpleNamespace(double_value=0.0),
                                       SimpleNamespace(integer_value=-1), SimpleNamespace(double_value=1.0)])
    def send(goal):
        """Record the generated goal and return a successful fake ROS handle."""
        calls.append(goal)
        return SimpleNamespace(accepted=True, get_result_async=lambda: SimpleNamespace(
            status=4, result=SimpleNamespace(json_output='{}')))
    client = SimpleNamespace(wait_for_server=lambda timeout_sec: True, send_goal_async=send)
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=1.0),
        parameters=SimpleNamespace(wait_for_services=lambda timeout_sec: True, get_parameters=get_parameters),
        structured_client=client, result=lambda future, timeout, operation: future)
    first = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
    second = Image(width=1, height=1, encoding='rgb8', step=3, data=[0, 0, 255])
    for frame in (first, second):
        assert describe_image(node, frame, 'Describe', run_id=memory.metadata['run_id'],
                              directory=memory.directory, pipeline=pipeline) == '{}'
    assert [bytes(goal.image.data) for goal in calls] == [bytes([255, 0, 0])] * 2
    assert all(goal.run_id == memory.metadata['run_id'] for goal in calls)
    with PILImage.open(memory.directory / 'observations/vision/frozen_image.png') as saved:
        assert saved.getpixel((0, 0)) == (255, 0, 0)


def test_identification_creates_persisted_refinement_queue(sense_module, tmp_path, monkeypatch):
    """Create refinement questions from mocked schema-constrained inference.

    An active run has a frozen camera image and a fixed initial observation.
    Verify the requested run and schema, ordered entity-property questions, image
    hash and identification provenance without loading an inference model.
    """
    from PIL import Image as PILImage
    from types import MethodType
    pipeline = {'enabled_actions': ['observe'], 'scene_refinement': {'enabled': True, 'properties': ['color', 'shape']},
        'stage_settings': {'sense': {'temperature': 0.0, 'max_output_tokens': 32, 'reasoning_effort': None,
                                    'seed': None, 'response_format': 'json_schema', 'output_schema': {'type': 'object'}}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    memory.begin_cycle()
    PILImage.new('RGB', (1, 1), (255, 0, 0)).save(memory.directory / 'observations/vision/frozen_image.png')
    monkeypatch.setattr(sense_module, 'GenerateStructured', SimpleNamespace(Goal=SimpleNamespace))
    calls = []
    def get_parameters(names):
        """Return matching experiment ownership and frozen generation values."""
        if names[0] == 'experiment_run_id':
            return SimpleNamespace(values=[SimpleNamespace(string_value=memory.metadata['run_id']),
                                           SimpleNamespace(string_value='fixture')])
        return SimpleNamespace(values=[SimpleNamespace(integer_value=32), SimpleNamespace(double_value=0.0),
                                       SimpleNamespace(integer_value=-1), SimpleNamespace(double_value=1.0)])
    def send(goal):
        """Return one fixed entity through a fake successful action result."""
        calls.append(goal)
        return SimpleNamespace(accepted=True, get_result_async=lambda: SimpleNamespace(status=4,
            result=SimpleNamespace(json_output='{"entities":[{"id":"cup","description":"red cup"}]}')))
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=1.0 if name == 'timeout' else str(tmp_path)),
        parameters=SimpleNamespace(wait_for_services=lambda timeout_sec: True, get_parameters=get_parameters),
        structured_text=SimpleNamespace(wait_for_server=lambda timeout_sec: True, send_goal_async=send),
        result=lambda future, timeout, operation: future)
    sense_module.SenseNode.identify_questions(node, 'visible cup', directory=memory.directory,
                                            run_id=memory.metadata['run_id'], pipeline=pipeline)
    state = json.loads((memory.directory / 'state/refinement.json').read_text())
    assert [item['property'] for item in state['questions']] == ['color', 'shape']
    assert all(not item['attempted'] for item in state['questions'])
    assert state['entities'][0]['id'] == 'cup' and len(state['image_sha256']) == 64
    assert calls[0].run_id == memory.metadata['run_id'] and json.loads(calls[0].json_schema)['required'] == ['entities']


def test_refinement_reuses_initial_observation_and_incorporates_focused_answer(sense_module, monkeypatch, tmp_path):
    """Reuse the first observation and carry new focused evidence into Sense.

    Set up a persisted refinement queue, prior successful Sense record and Act
    observation. The next Sense call must not poll the sensors again, and its
    output and provenance must include the focused answer exactly once.
    """
    pipeline = {'enabled_actions': ['observe'], 'scene_refinement': {'enabled': True, 'properties': ['color']},
        'sensing': {'vision': True}, 'stage_prompts': {'sense': {'name': 'chosen', 'text': 'Describe.'}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    memory.begin_cycle()
    memory.write_refinement({'status': 'running', 'questions': []})
    cycle = memory.directory / 'state/cycle_000001'
    initial = '{"vision":"cup"}'
    (cycle / 'sense.json').write_text(json.dumps({'stage': 'sense', 'status': 'generated', 'sequence': 1,
                                                  'invocation': 1, 'output': initial, 'source_observations': []}))
    source = 'observations/vision/observe_000001_001.json'
    (memory.directory / source).write_text(json.dumps({'target': 'cup', 'output': 'red'}))
    (cycle / 'act.json').write_text(json.dumps({'stage': 'act', 'status': 'succeeded', 'sequence': 2,
                                                'invocation': 1, 'observation_record': source}))
    def unexpected(*args, **kwargs):
        """Fail if refinement attempts to collect the original sensory inputs again."""
        raise AssertionError('sensors must not be polled again')
    monkeypatch.setattr(sense_module, 'observe', unexpected)
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=1.0 if name == 'timeout' else str(tmp_path)),
                           wait=Mock(), describe=Mock(), get_logger=lambda: Mock())
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id']), succeed=Mock(), abort=Mock())
    result = sense_module.SenseNode.execute(node, handle)
    handle.succeed.assert_called_once()
    output = json.loads(result.observation)
    assert output['initial_observation'] == initial
    assert output['focused_observations'][0]['source_record'] == source
    record = json.loads((memory.directory / result.record_path).read_text())
    assert record['source_observations'] == [source] and record['output'] == result.observation
