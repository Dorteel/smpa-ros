"""Exercise Plan callback behavior without requiring generated Plan type support."""
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
def plan_node_class(monkeypatch):
    """Import Plan against a minimal fake result while ROS generation is deferred.

    Only the unavailable new Plan message is replaced. The production callback,
    planner, run memory and existing structured inference schema remain real.
    Restore the module after each test so this fake cannot affect other tests.
    """
    class Result:
        """Retain the action JSON and provenance fields returned by Plan."""
        def __init__(self):
            """Start both result fields empty until planning succeeds."""
            self.action_json = ''
            self.record_path = ''

    monkeypatch.setattr(action, 'Plan', SimpleNamespace(Result=Result), raising=False)
    sys.modules.pop('smpa_ros.plan_node', None)
    module = importlib.import_module('smpa_ros.plan_node')
    yield module.PlanNode
    sys.modules.pop('smpa_ros.plan_node', None)


def test_plan_callback_preserves_decision_and_provenance(plan_node_class, tmp_path):
    """Run the real planner once without starting an inference model or stage.

    Provide an already persisted source record and a fixed structured response.
    Check the returned JSON, schema forwarded to the mock callable, and the
    saved source and prompt provenance. The callback must finish successfully.
    """
    pipeline = {'enabled_actions': ['look_left'],
                'stage_prompts': {'plan': {'name': 'chosen', 'text': '{goal} {input} {available_actions}'}}}
    memory = MemoryManager(tmp_path, goal='Find the cup', configuration={'pipeline': pipeline})
    memory.begin_cycle()
    source = memory.directory / 'state/cycle_000001/model.json'
    source.write_text(json.dumps({'output': 'visible cup'}))
    calls = []

    def generate(prompt, **settings):
        """Record the real planner's schema and return one allowed action."""
        calls.append((prompt, settings))
        return '{"action":"look_left"}'

    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path)),
                           generate=generate, get_logger=lambda: Mock())
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'],
                               source_record='state/cycle_000001/model.json', world_model='visible cup'),
                             succeed=Mock(), abort=Mock())
    result = plan_node_class.execute(node, handle)
    handle.succeed.assert_called_once()
    assert json.loads(result.action_json) == {'action': 'look_left'}
    record = json.loads((memory.directory / result.record_path).read_text())
    assert record['source_record'] == 'state/cycle_000001/model.json'
    assert record['prompt_name'] == 'chosen'
    assert calls[0][1]['schema']['properties']['action']['enum'] == ['look_left']


def test_plan_callback_records_inference_failure(plan_node_class, tmp_path):
    """Abort a failed planner request and retain its failure record.

    The mocked inference callable raises before producing a decision. The real
    planner must persist its error under the reserved invocation and the action
    callback must abort without claiming an action or invoking another stage.
    """
    memory = MemoryManager(tmp_path, goal='Find the cup')
    memory.begin_cycle()

    def fail(prompt, **settings):
        """Simulate an unavailable inference action without loading a model."""
        raise RuntimeError('fixture unavailable')

    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path)),
                           generate=fail, get_logger=lambda: Mock())
    handle = SimpleNamespace(request=SimpleNamespace(run_id=memory.metadata['run_id'],
                               source_record='', world_model='visible cup'),
                             succeed=Mock(), abort=Mock())
    result = plan_node_class.execute(node, handle)
    handle.abort.assert_called_once()
    assert not result.action_json
    record = json.loads((memory.directory / 'state/cycle_000001/plan.json').read_text())
    assert record['status'] == 'failed' and 'fixture unavailable' in record['error']


def test_plan_goal_capability_selection(plan_node_class):
    """Accept the configured default and explicit Plan capability only.

    Empty and explicit `plan` select planning; an unrelated capability is
    rejected before any inference call or run record can be allocated.
    """
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value='plan'), get_logger=lambda: Mock())
    assert plan_node_class.accept_goal(node, SimpleNamespace(capability='')) == GoalResponse.ACCEPT
    assert plan_node_class.accept_goal(node, SimpleNamespace(capability='plan')) == GoalResponse.ACCEPT
    assert plan_node_class.accept_goal(node, SimpleNamespace(capability='sense')) == GoalResponse.REJECT


def test_plan_requires_matching_inference_freeze(plan_node_class):
    """Reject an inference server frozen for a different experiment.

    Supply fake parameter responses and deliberately avoid providing an action
    client. The real generation method must fail before it sends any inference
    goal, even if the other server settings could otherwise match.
    """
    parameters = SimpleNamespace(wait_for_services=lambda timeout_sec: True,
        get_parameters=lambda names: SimpleNamespace(values=[SimpleNamespace(string_value='other-run'),
                                                               SimpleNamespace(string_value='qwen3_4b')]))
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=1.0), parameters=parameters,
                           wait=lambda future, timeout, operation: future)
    with pytest.raises(RuntimeError, match='not frozen for this experiment'):
        plan_node_class.generate(node, 'prompt', run_id='run-a', response_format='json_schema', schema={})


def test_plan_uses_its_saved_stage_settings(plan_node_class, tmp_path, monkeypatch):
    """Send Plan's frozen controls and stage identity to structured inference.

    Give Plan settings distinct from Model, persist the exact run snapshot, and
    check the generated goal and schema. A mismatched requested control must
    fail without sending another action.
    """
    import smpa_ros.plan_node as module
    from action_msgs.msg import GoalStatus
    memory = MemoryManager(tmp_path, goal='Find cup')
    memory.begin_cycle()
    controls = dict(max_new_tokens=83, temperature=0.3, seed=-1, top_p=1.0, reasoning_effort='high')
    snapshot = dict(run_id=memory.metadata['run_id'], active_model='fixture',
                    stages={'plan': controls, 'model': dict(controls, max_new_tokens=41)})
    memory.metadata['inference'] = snapshot
    memory.write_metadata()
    outgoing = []
    def get_parameters(names):
        """Return the saved ownership and authoritative frozen snapshot."""
        return SimpleNamespace(values=[SimpleNamespace(string_value=memory.metadata['run_id']),
            SimpleNamespace(string_value='fixture'), SimpleNamespace(string_value=json.dumps(snapshot))])
    def send(goal):
        """Capture the request and return a successful structured fixture."""
        outgoing.append(goal)
        return SimpleNamespace(accepted=True, get_result_async=lambda: SimpleNamespace(
            status=GoalStatus.STATUS_SUCCEEDED, result=SimpleNamespace(json_output='{}')))
    monkeypatch.setattr(module, 'GenerateStructured', SimpleNamespace(Goal=SimpleNamespace))
    node = SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value=str(tmp_path) if name == 'memory_root' else 1.0),
        parameters=SimpleNamespace(wait_for_services=lambda timeout_sec: True, get_parameters=get_parameters),
        client=SimpleNamespace(wait_for_server=lambda timeout_sec: True, send_goal_async=send),
        wait=lambda future, timeout, operation: future)
    assert plan_node_class.generate(node, 'prompt', run_id=memory.metadata['run_id'],
        max_new_tokens=83, temperature=0.3, reasoning_effort='high',
        response_format='json_schema', schema={'type': 'object'}) == '{}'
    assert outgoing[0].stage == 'plan' and json.loads(outgoing[0].json_schema) == {'type': 'object'}
    with pytest.raises(ValueError, match='do not match'):
        plan_node_class.generate(node, 'prompt', run_id=memory.metadata['run_id'],
            max_new_tokens=41, temperature=0.3, reasoning_effort='high',
            response_format='json_schema', schema={'type': 'object'})
    assert len(outgoing) == 1
