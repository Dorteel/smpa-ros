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
    monkeypatch.setattr(action, 'GenerateStructuredChat', SimpleNamespace(Goal=SimpleNamespace), raising=False)
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
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value='model' if name == 'default_capability' else str(tmp_path)),
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


def test_model_completes_refinement_after_all_questions_attempted(model_node_class, tmp_path):
    """Mark refinement complete only after its answers have reached Model.

    Provide an already attempted queue and a mocked world-model generation.
    The real Model action persists its snapshot before finalizing the queue.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    memory.begin_cycle()
    memory.write_refinement({'status': 'running', 'questions': [{'attempted': True}]})
    def generate(prompt, **settings):
        """Return a fixed world model without invoking a model backend."""
        return 'world'
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value='model' if name == 'default_capability' else str(tmp_path)),
                           generate=generate, get_logger=lambda: Mock())
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'], observation='cup',
                               source_record='', previous_snapshot=''), succeed=Mock(), abort=Mock())
    model_node_class.execute(node, handle)
    handle.succeed.assert_called_once()
    assert json.loads((memory.directory / 'state/refinement.json').read_text())['status'] == 'completed'


def test_belief_capability_reuses_writer_and_frozen_settings(model_node_class, tmp_path, monkeypatch):
    """Route a conversation history to the existing writer under its run freeze.

    Attach a real temporary memory run, but replace belief generation and graph
    updates. Check that message roles and recorded model settings reach the writer
    and that the action succeeds without invoking world-model generation.
    """
    import smpa_ros.model_node as module
    from smpa_ros.conversation import open_conversation, append_message
    memory = open_conversation(tmp_path)
    memory.begin_cycle()
    append_message(memory.directory, 'User', 'hello')
    append_message(memory.directory, 'Assistant', 'hi')
    snapshot = dict(run_id=memory.metadata['run_id'], server_instance_id='server',
                    active_model='fixture', loading={'backend': 'transformers', 'model_id': 'fixture'},
                    generation={'temperature': 0.0, 'max_new_tokens': 42, 'top_p': 1.0, 'seed': -1})
    memory.metadata['inference'] = snapshot
    memory.write_metadata()
    calls = []
    def model_messages(attached, model, messages):
        """Capture the attached identity, settings, and history without inference."""
        calls.append((attached.metadata['run_id'], model, messages))
    monkeypatch.setattr(module, 'model_messages', model_messages)
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path) if name == 'memory_root' else ''),
                           generate_belief=Mock(), get_logger=lambda: Mock())
    node.update_beliefs = lambda request: model_node_class.update_beliefs(node, request)
    history = [{'role': 'user', 'content': 'hello'}, {'role': 'assistant', 'content': 'hi'}]
    goal = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'], capability='belief',
                           messages_json=json.dumps(history)), succeed=Mock(), abort=Mock())
    model_node_class.execute(node, goal)
    goal.succeed.assert_called_once()
    assert calls[0][0] == memory.metadata['run_id'] and calls[0][2] == history
    assert calls[0][1]['settings'] == snapshot['generation']
    assert calls[0][1]['agent'] == snapshot['loading']


def test_belief_rejects_unfrozen_run(model_node_class, tmp_path):
    """Refuse a belief update when no authoritative frozen settings were saved.

    The existing run has an active cycle, but no inference snapshot. No writer
    or inference operation may be started for that incomplete run context.
    """
    memory = MemoryManager(tmp_path, goal='Conversation')
    memory.begin_cycle()
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path)))
    request = SimpleNamespace(run_id=memory.metadata['run_id'], messages_json='[]')
    with pytest.raises(RuntimeError, match='no recorded frozen'):
        model_node_class.update_beliefs(node, request)


def test_belief_rejects_history_that_differs_from_transcript(model_node_class, tmp_path):
    """Reject history that would attribute invented text to the saved transcript.

    Persist a real conversation message and a frozen inference snapshot, then
    supply a different message. The belief writer must not be reached.
    """
    from smpa_ros.conversation import open_conversation, append_message
    memory = open_conversation(tmp_path)
    memory.begin_cycle()
    append_message(memory.directory, 'User', 'actual')
    memory.metadata['inference'] = dict(run_id=memory.metadata['run_id'], loading={}, generation={})
    memory.write_metadata()
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path)))
    request = SimpleNamespace(run_id=memory.metadata['run_id'],
                              messages_json=json.dumps([{'role': 'user', 'content': 'invented'}]))
    with pytest.raises(ValueError, match='does not match'):
        model_node_class.update_beliefs(node, request)
