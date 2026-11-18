"""Exercise the experiment's explicit action order with mocked stage clients."""
import json
from types import SimpleNamespace

import pytest
from memory import MemoryManager
from smpa_ros_interfaces import action
from smpa_ros.smpa import run_actions


@pytest.fixture
def stage_types(monkeypatch):
    """Supply ungenerated goal classes without starting cognitive ROS nodes.

    The goal constructor keeps request fields unchanged so tests can verify
    run identity, provenance and previous world-model propagation.
    """
    for name in ('Sense', 'Model', 'Plan', 'Act'):
        monkeypatch.setattr(action, name, SimpleNamespace(Goal=SimpleNamespace), raising=False)


def test_explicit_sequence_and_provenance(stage_types, tmp_path):
    """Run two complete cycles and check every action's inputs and ordering.

    Mock stage outcomes and confirm each persisted record is handed to the next
    request, the second cycle receives the first world-model snapshot, and both
    cycles finish only after their Act result succeeds.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    calls, events = [], []
    def invoke(stage, action_type, goal):
        """Return distinct deterministic stage outputs without ROS execution."""
        calls.append((stage, goal))
        number = len([item for item in calls if item[0] == stage])
        if stage == 'sense':
            return SimpleNamespace(observation=f'observation-{number}', record_path=f'sense-{number}')
        if stage == 'model':
            return SimpleNamespace(world_model=f'world-{number}', record_path=f'model-{number}', snapshot_path=f'snapshot-{number}')
        if stage == 'plan':
            return SimpleNamespace(action_json='{"action":"look_left"}', record_path=f'plan-{number}')
        return SimpleNamespace(success=True)
    def notify(stage, event):
        """Retain lifecycle events in their actual publication order."""
        events.append((stage, event))
    run_actions(memory, 2, invoke, notify=notify)
    assert [name for name, _ in calls] == ['sense', 'model', 'plan', 'act'] * 2
    assert calls[1][1].source_record == 'sense-1' and calls[1][1].previous_snapshot == ''
    assert calls[2][1].source_record == 'model-1' and calls[3][1].source_record == 'plan-1'
    assert calls[5][1].previous_snapshot == 'snapshot-1'
    assert all(goal.run_id == memory.metadata['run_id'] for _, goal in calls)
    assert memory.metadata['completed_cycles'] == 2 and len(events) == 16


def test_failed_action_keeps_cycle_incomplete(stage_types, tmp_path):
    """Leave the active cycle incomplete when a stage returns a failure.

    Fail the Act result after the three earlier mocked stages. The experiment
    reports the actual failed stage and never advances its completed-cycle count.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    events = []
    def invoke(stage, action_type, goal):
        """Return minimal successful data until Act reports a failure."""
        return {'sense': SimpleNamespace(observation='seen', record_path='sense'),
                'model': SimpleNamespace(world_model='world', record_path='model', snapshot_path='snapshot'),
                'plan': SimpleNamespace(action_json=json.dumps({'action': 'look_left'}), record_path='plan'),
                'act': SimpleNamespace(success=False)}[stage]
    def notify(stage, event):
        """Capture the failed stage notification for the lifecycle observer."""
        events.append((stage, event))
    with pytest.raises(RuntimeError, match='Act did not complete'):
        run_actions(memory, 1, invoke, notify=notify)
    assert memory.metadata['completed_cycles'] == 0 and not memory.in_cycle
    assert events[-1] == ('act', 'stage_failed')


def test_sequence_runs_real_stage_callbacks_with_mocked_dependencies(monkeypatch, tmp_path):
    """Execute the full script sequence against real stage callback functions.

    Fake only the ungenerated action messages, inference, sensors and robot
    operation. The actual stage implementations and run memory write their
    normal records. Confirm source links and cycle completion across all stages.
    """
    import importlib
    import sys
    from unittest.mock import Mock
    from smpa_ros_interfaces import action

    fields = {'Sense': ('observation', 'record_path'), 'Model': ('world_model', 'record_path', 'snapshot_path'),
              'Plan': ('action_json', 'record_path'), 'Act': ('success', 'record_path')}
    for name, names in fields.items():
        def result_factory(names=names):
            """Create empty result fields matching one ungenerated action."""
            return SimpleNamespace(**{key: False if key == 'success' else '' for key in names})
        monkeypatch.setattr(action, name, SimpleNamespace(Goal=SimpleNamespace, Result=result_factory), raising=False)
    module_names = ['smpa_ros.sense_node', 'smpa_ros.model_node', 'smpa_ros.plan_node', 'smpa_ros.act_node']
    for name in module_names:
        sys.modules.pop(name, None)
    sense, model, plan, act = [importlib.import_module(name) for name in module_names]
    pipeline = {'enabled_actions': ['look_left'], 'sensing': {'vision': False},
        'stage_prompts': {'sense': {'name': 'chosen', 'text': 'Sense.'},
                          'model': {'name': 'chosen', 'text': '{input} {previous_world_model}'},
                          'plan': {'name': 'chosen', 'text': '{goal} {input} {available_actions}'},
                          'act': {'name': 'chosen', 'text': '{input}'}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    def observe_once(*args, **kwargs):
        """Provide a fixed sensory observation without touching hardware."""
        return '{"vision":"cup"}', ''
    monkeypatch.setattr(sense, 'observe', observe_once)
    def world(prompt, **settings):
        """Return a fixed world model without loading a text model."""
        return 'world with cup'
    def decision(prompt, **settings):
        """Return a valid fixed decision without loading a planner."""
        return '{"action":"look_left"}'
    def dispatch(name, record, **context):
        """Report a successful robot operation without starting simulation."""
        record.update(goal_accepted=True, result_status=4, success=True)
    def parameter(name):
        """Provide each stage its memory root and bounded timeout."""
        return SimpleNamespace(value=1.0 if name == 'timeout' else str(tmp_path))
    nodes = {'sense': SimpleNamespace(get_parameter=parameter, wait=Mock(), describe=Mock(), get_logger=lambda: Mock()),
             'model': SimpleNamespace(get_parameter=parameter, generate=world, get_logger=lambda: Mock()),
             'plan': SimpleNamespace(get_parameter=parameter, generate=decision, get_logger=lambda: Mock()),
             'act': SimpleNamespace(get_parameter=parameter, dispatch=dispatch, get_logger=lambda: Mock())}
    callbacks = {'sense': sense.SenseNode.execute, 'model': model.ModelNode.execute,
                 'plan': plan.PlanNode.execute, 'act': act.ActNode.execute}
    def invoke(stage, action_type, goal):
        """Deliver one action goal to the real stage callback and check success."""
        handle = SimpleNamespace(request=goal, succeed=Mock(), abort=Mock())
        result = callbacks[stage](nodes[stage], handle)
        handle.succeed.assert_called_once()
        handle.abort.assert_not_called()
        return result
    try:
        run_actions(memory, 1, invoke)
        directory = memory.directory / 'state/cycle_000001'
        records = {name: json.loads((directory / f'{name}.json').read_text())
                   for name in ('sense', 'model', 'plan', 'act')}
        assert records['model']['source_record'] == 'state/cycle_000001/sense.json'
        assert records['plan']['source_record'] == 'state/cycle_000001/model.json'
        assert records['act']['source_record'] == 'state/cycle_000001/plan.json'
        assert records['act']['status'] == 'succeeded' and memory.metadata['completed_cycles'] == 1
    finally:
        for name in module_names:
            sys.modules.pop(name, None)
