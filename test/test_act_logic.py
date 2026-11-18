"""Check the independent Act callback with a mocked robot action."""
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
def act_node_class(monkeypatch):
    """Replace the ungenerated Act result while exercising actual stage logic.

    Restore the module after each test. Robot actions are provided by the test,
    so no simulation or inference model is started.
    """
    class Result:
        """Carry the action outcome and provenance path."""
        def __init__(self):
            """Initialize the action as unsuccessful until its callback succeeds."""
            self.success = False
            self.record_path = ''
    monkeypatch.setattr(action, 'Act', SimpleNamespace(Result=Result), raising=False)
    sys.modules.pop('smpa_ros.act_node', None)
    module = importlib.import_module('smpa_ros.act_node')
    yield module.ActNode
    sys.modules.pop('smpa_ros.act_node', None)


def test_act_persists_robot_action_and_provenance(act_node_class, tmp_path):
    """Run the real Act persistence path with a successful mock robot endpoint.

    Confirm that the selected action and its source are preserved in the record
    and that the callback reports the underlying success to its caller.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    memory.begin_cycle()
    (memory.directory / 'state/cycle_000001/plan.json').write_text('{}')
    calls = []
    def dispatch(action_name, record, **context):
        """Record the requested movement and supply a successful robot result."""
        calls.append(action_name)
        record.update(goal_accepted=True, result_status=4, success=True)
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path)),
                           dispatch=dispatch, get_logger=lambda: Mock())
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'],
        action_json='{"action":"look_left"}', source_record='state/cycle_000001/plan.json'),
        succeed=Mock(), abort=Mock())
    result = act_node_class.execute(node, handle)
    handle.succeed.assert_called_once()
    assert result.success and calls == ['look_left']
    record = json.loads((memory.directory / result.record_path).read_text())
    assert record['source_record'] == 'state/cycle_000001/plan.json'
    assert record['status'] == 'succeeded' and record['requested_action'] == 'look_left'


def test_act_failure_and_capability(act_node_class, tmp_path):
    """Reject unsupported capabilities and preserve failed execution records.

    A failing mocked robot endpoint causes an aborted action, never a claimed
    success. Both the configured default and explicit Act selection are accepted.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    memory.begin_cycle()
    def fail(action_name, record, **context):
        """Simulate an unavailable robot endpoint without starting simulation."""
        raise RuntimeError('robot unavailable')
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value='act' if name == 'default_capability' else str(tmp_path)),
                           dispatch=fail, get_logger=lambda: Mock())
    assert act_node_class.accept_goal(node, SimpleNamespace(capability='')) == GoalResponse.ACCEPT
    assert act_node_class.accept_goal(node, SimpleNamespace(capability='act')) == GoalResponse.ACCEPT
    assert act_node_class.accept_goal(node, SimpleNamespace(capability='plan')) == GoalResponse.REJECT
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'],
        action_json='{"action":"look_left"}', source_record=''), succeed=Mock(), abort=Mock())
    result = act_node_class.execute(node, handle)
    handle.abort.assert_called_once()
    assert not result.success
    record = json.loads((memory.directory / 'state/cycle_000001/act.json').read_text())
    assert record['status'] == 'failed' and 'robot unavailable' in record['error']


def test_focused_observation_uses_saved_camera_evidence(act_node_class, tmp_path, monkeypatch):
    """Run Observe against a saved image and a mocked shared inference call.

    Persist a prior Sense record and image, then execute the real Act and focus
    logic. Confirm the saved image and question reach inference and the answer
    is attributed to a durable observation and action record.
    """
    from types import MethodType
    from PIL import Image as PILImage
    import smpa_ros.sense as sensing
    pipeline = {'enabled_actions': ['observe'], 'stage_settings': {'sense': {'temperature': 0.0,
        'max_output_tokens': 32, 'reasoning_effort': None, 'seed': None, 'response_format': 'text'}},
        'stage_prompts': {'sense': {'name': 'chosen', 'text': 'Describe.'}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    memory.begin_cycle()
    image = memory.directory / 'observations/vision/fixture.png'
    PILImage.new('RGB', (1, 1), (255, 0, 0)).save(image)
    (memory.directory / 'state/cycle_000001/sense.json').write_text(json.dumps({
        'status': 'generated', 'sequence': 1, 'source_observations': ['observations/vision/fixture.png']}))
    calls = []
    def describe(node, frame, prompt, *, run_id, directory, pipeline):
        """Capture the question and image without invoking a real model."""
        calls.append((bytes(frame.data), prompt, run_id))
        return 'red'
    monkeypatch.setattr(sensing, 'describe_image', describe)
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path)), get_logger=lambda: Mock())
    node.dispatch = MethodType(act_node_class.dispatch, node)
    node.focus = MethodType(act_node_class.focus, node)
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'],
        action_json=json.dumps({'action': 'observe', 'target': None, 'question': 'What color?'}), source_record=''),
        succeed=Mock(), abort=Mock())
    result = act_node_class.execute(node, handle)
    handle.succeed.assert_called_once()
    record = json.loads((memory.directory / result.record_path).read_text())
    observation = json.loads((memory.directory / record['observation_record']).read_text())
    assert observation['output'] == 'red' and observation['question'] == 'What color?'
    assert calls[0][0] == bytes([255, 0, 0]) and calls[0][2] == memory.metadata['run_id']


def test_focused_refinement_persists_answer_and_queue(act_node_class, tmp_path, monkeypatch):
    """Update a refinement question after mocked structured image inference.

    An existing frozen image and unattempted question belong to the active run.
    The actual Act callback must save the answer and source path, mark the queue
    entry attempted, and forward the required response schema to inference.
    """
    from types import MethodType
    from PIL import Image as PILImage
    import smpa_ros.sense as sensing
    pipeline = {'enabled_actions': ['observe'], 'scene_refinement': {'enabled': True, 'properties': ['color']},
        'stage_settings': {'sense': {'temperature': 0.0, 'max_output_tokens': 32, 'reasoning_effort': None,
                                    'seed': None, 'response_format': 'json_schema', 'output_schema': {'type': 'object'}}},
        'stage_prompts': {'sense': {'name': 'chosen', 'text': 'Describe.'}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    memory.begin_cycle()
    PILImage.new('RGB', (1, 1), (255, 0, 0)).save(memory.directory / 'observations/vision/frozen_image.png')
    memory.write_refinement({'status': 'running', 'questions': [{'target': 'cup', 'question': 'What color?',
                                                                  'attempted': False, 'status': 'unanswered', 'answer': None}]})
    schemas = []
    def describe(node, frame, prompt, *, run_id, directory, pipeline):
        """Return a valid structured answer and retain its requested schema."""
        schemas.append(pipeline['stage_settings']['sense']['output_schema'])
        return '{"status":"answered","answer":"red"}'
    monkeypatch.setattr(sensing, 'describe_image', describe)
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path)), get_logger=lambda: Mock())
    node.dispatch = MethodType(act_node_class.dispatch, node)
    node.focus = MethodType(act_node_class.focus, node)
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'],
        action_json=json.dumps({'action': 'observe', 'target': 'cup', 'question': 'What color?'}), source_record=''),
        succeed=Mock(), abort=Mock())
    result = act_node_class.execute(node, handle)
    handle.succeed.assert_called_once()
    record = json.loads((memory.directory / result.record_path).read_text())
    entry = json.loads((memory.directory / 'state/refinement.json').read_text())['questions'][0]
    assert entry['attempted'] and entry['status'] == 'answered' and entry['answer'] == 'red'
    assert entry['source_record'] == record['observation_record']
    assert schemas[0]['required'] == ['status', 'answer']


def test_focused_refinement_failure_retains_attempt(act_node_class, tmp_path, monkeypatch):
    """Retain an attempted unanswered question when image inference fails.

    The action aborts and persists its failure record. The refinement queue keeps
    the attempted question and error so a failure is not silently retried as a
    new observation or reported as a successful answer.
    """
    from types import MethodType
    from PIL import Image as PILImage
    import smpa_ros.sense as sensing
    pipeline = {'enabled_actions': ['observe'], 'scene_refinement': {'enabled': True, 'properties': ['color']},
        'stage_settings': {'sense': {'temperature': 0.0, 'max_output_tokens': 32, 'reasoning_effort': None,
                                    'seed': None, 'response_format': 'json_schema', 'output_schema': {'type': 'object'}}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    memory.begin_cycle()
    PILImage.new('RGB', (1, 1), (255, 0, 0)).save(memory.directory / 'observations/vision/frozen_image.png')
    memory.write_refinement({'status': 'running', 'questions': [{'target': 'cup', 'question': 'What color?',
                                                                  'attempted': False, 'status': 'unanswered', 'answer': None}]})
    def fail(*args, **kwargs):
        """Simulate a failed shared inference request without loading a model."""
        raise RuntimeError('vision unavailable')
    monkeypatch.setattr(sensing, 'describe_image', fail)
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path)), get_logger=lambda: Mock())
    node.dispatch = MethodType(act_node_class.dispatch, node)
    node.focus = MethodType(act_node_class.focus, node)
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'],
        action_json=json.dumps({'action': 'observe', 'target': 'cup', 'question': 'What color?'}), source_record=''),
        succeed=Mock(), abort=Mock())
    act_node_class.execute(node, handle)
    handle.abort.assert_called_once()
    entry = json.loads((memory.directory / 'state/refinement.json').read_text())['questions'][0]
    record = json.loads((memory.directory / 'state/cycle_000001/act.json').read_text())
    assert entry['attempted'] and entry['status'] == 'unanswered' and 'vision unavailable' in entry['error']
    assert record['status'] == 'failed' and not record['success']


def test_demo_talk_uses_assigned_run_and_latest_transcript(act_node_class, tmp_path, monkeypatch):
    """Repeat the persisted Hear transcript through the run-specific Talk action.

    Mock the ROS client while exercising the real Act speech helper. The goal
    must use the original demo phrase and the client must be cleaned up.
    """
    import sys
    memory = MemoryManager(tmp_path, goal='Listen')
    memory.begin_cycle()
    (memory.directory / 'state/cycle_000001/sense.json').write_text(json.dumps({
        'status': 'generated', 'sequence': 1, 'output': '{"speech":"hello"}'}))
    calls, endpoints = [], []
    def send(goal):
        """Record one mock Talk goal and return successful completion."""
        calls.append(goal.text)
        return SimpleNamespace(accepted=True, get_result_async=lambda: SimpleNamespace(
            status=4, result=SimpleNamespace(success=True, error_message='')))
    client = SimpleNamespace(wait_for_server=lambda timeout_sec: True, send_goal_async=send, destroy=Mock())
    def action_client(node, kind, endpoint, callback_group):
        """Record which Talk endpoint belongs to this experiment run."""
        endpoints.append(endpoint)
        return client
    monkeypatch.setattr(sys.modules[act_node_class.__module__], 'ActionClient', action_client)
    node = SimpleNamespace(group=object(), result=lambda future, timeout, operation: future, get_logger=lambda: Mock())
    act_node_class.speak_transcript(node, memory.directory, memory.metadata['run_id'], 2, 1.0)
    assert calls == ['I heard hello']
    assert endpoints == ['/run_' + memory.metadata['run_id'].replace('-', '_') + '/act/talk']
    client.destroy.assert_called_once()
