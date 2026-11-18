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
