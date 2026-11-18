"""Exercise Model callbacks without regenerated action type support."""
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
def model_node_class(monkeypatch):
    """Import the real Model callback with only its generated result replaced.

    Restore module state afterwards so the temporary stand-in cannot affect
    other tests. No inference model or running ROS stage is required.
    """
    class Result:
        """Hold the world-model text and persisted provenance paths."""
        def __init__(self):
            """Start the result empty until a callback completes successfully."""
            self.world_model = ''
            self.record_path = ''
            self.snapshot_path = ''
    monkeypatch.setattr(action, 'Model', SimpleNamespace(Result=Result), raising=False)
    sys.modules.pop('smpa_ros.model_node', None)
    module = importlib.import_module('smpa_ros.model_node')
    yield module.ModelNode
    sys.modules.pop('smpa_ros.model_node', None)


def test_model_persists_source_and_previous_snapshot(model_node_class, tmp_path):
    """Generate through the real Model function using a mocked inference call.

    Supply an existing source and previous snapshot, then verify the generated
    prompt, world-model snapshot and provenance. No other stage is invoked.
    """
    pipeline = {'stage_prompts': {'model': {'name': 'chosen', 'text': '{input} | {previous_world_model}'}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    memory.begin_cycle()
    (memory.directory / 'state/cycle_000001/sense.json').write_text(json.dumps({'source_observations': ['observations/vision/example.png']}))
    (memory.directory / 'world_model/previous.md').write_text('previous view')
    calls = []
    def generate(prompt, **settings):
        """Capture the generated prompt and return a fixed model response."""
        calls.append((prompt, settings))
        return 'new view'
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path)),
                           generate=generate, get_logger=lambda: Mock())
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'], observation='a cup',
                               source_record='state/cycle_000001/sense.json', previous_snapshot='world_model/previous.md'),
                             succeed=Mock(), abort=Mock())
    result = model_node_class.execute(node, handle)
    handle.succeed.assert_called_once()
    record = json.loads((memory.directory / result.record_path).read_text())
    assert result.world_model == 'new view'
    assert (memory.directory / result.snapshot_path).read_text() == 'new view'
    assert record['source_record'] == 'state/cycle_000001/sense.json'
    assert record['source_observations'] == ['state/cycle_000001/sense.json', 'observations/vision/example.png']
    assert record['previous_iteration'] == 'world_model/previous.md'
    assert calls[0][0] == 'a cup | previous view'


def test_model_failure_and_capability(model_node_class, tmp_path):
    """Reject unsupported capabilities and persist an inference failure.

    The default and explicit Model operations are accepted. A failing mocked
    inference call aborts the action and leaves its reserved failure record.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    memory.begin_cycle()
    def fail(prompt, **settings):
        """Simulate an unavailable inference backend."""
        raise RuntimeError('inference unavailable')
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value='model' if name == 'default_capability' else str(tmp_path)),
                           generate=fail, get_logger=lambda: Mock())
    assert model_node_class.accept_goal(node, SimpleNamespace(capability='')) == GoalResponse.ACCEPT
    assert model_node_class.accept_goal(node, SimpleNamespace(capability='model')) == GoalResponse.ACCEPT
    assert model_node_class.accept_goal(node, SimpleNamespace(capability='sense')) == GoalResponse.REJECT
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'], observation='cup',
                               source_record='', previous_snapshot=''), succeed=Mock(), abort=Mock())
    model_node_class.execute(node, handle)
    handle.abort.assert_called_once()
    record = json.loads((memory.directory / 'state/cycle_000001/model.json').read_text())
    assert record['status'] == 'failed' and 'inference unavailable' in record['error']


def test_model_requires_matching_inference_freeze(model_node_class):
    """Reject a shared inference server owned by another experiment.

    A fake parameter response identifies the other run. The Model adapter must
    fail before it can send a generation goal or claim matching provenance.
    """
    parameters = SimpleNamespace(wait_for_services=lambda timeout_sec: True,
        get_parameters=lambda names: SimpleNamespace(values=[SimpleNamespace(string_value='other-run'),
                                                               SimpleNamespace(string_value='qwen3_4b')]))
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=1.0), parameters=parameters,
                           wait=lambda future, timeout, operation: future)
    with pytest.raises(RuntimeError, match='not frozen for this experiment'):
        model_node_class.generate(node, 'prompt', run_id='run-a', response_format='text')
