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
    def dispatch(action_name, record, pipeline):
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
    def fail(action_name, record, pipeline):
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
