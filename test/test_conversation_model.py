"""Check conversation's Model delegation without ROS action type generation."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import rclpy
import rclpy.action
import rclpy.node
import rclpy.executors
import rclpy.parameter_client
import smpa_ros_interfaces.action as actions
import smpa_ros.smpa as smpa
from smpa_ros import conversation_web


@pytest.mark.parametrize('unconfirmed', [False, True])
def test_conversation_delegates_history_and_releases_freeze(monkeypatch, tmp_path, unconfirmed):
    """Acquire the run freeze, send both histories to Model, and release it.

    Substitute only transport and lifecycle boundaries. Keep the actual exchange
    adapter so it validates settings, sends role-preserving input and confirms
    cleanup without loading a model or starting a ROS server.
    """
    events = []
    memory = SimpleNamespace(directory=tmp_path/'runs'/'run', metadata={'run_id': 'run'}, write_metadata=Mock())
    snapshot = {'loading': {'model_id': 'fixture'}, 'generation': {'max_new_tokens': 42, 'temperature': 0.0, 'top_p': 1.0, 'seed': -1}}
    monkeypatch.setattr(actions, 'Model', SimpleNamespace(Goal=SimpleNamespace), raising=False)
    monkeypatch.setattr(rclpy, 'init', lambda **kwargs: None)
    monkeypatch.setattr(rclpy, 'shutdown', lambda **kwargs: events.append('shutdown'))
    monkeypatch.setattr(rclpy.executors, 'SingleThreadedExecutor', lambda **kwargs: SimpleNamespace(add_node=lambda node: None, spin_until_future_complete=lambda future, timeout_sec: None, shutdown=lambda: events.append('executor_shutdown')))
    monkeypatch.setattr(rclpy.node, 'Node', lambda *a, **k: SimpleNamespace(destroy_node=lambda: events.append('node_destroy')))
    monkeypatch.setattr(rclpy.action, 'ActionClient', lambda *a, **k: SimpleNamespace(destroy=lambda: events.append('client_destroy')))
    class Parameters:
        """Return the expected Model root and ontology for this test run."""
        def __init__(self, node, name):
            """Keep the service name so each fake client remains distinct."""
            self.name = name
        def wait_for_services(self, timeout_sec):
            """Report immediate service discovery without networking."""
            return True
        def get_parameters(self, names):
            """Return Model's matching root and empty default ontology."""
            return SimpleNamespace(done=lambda: True, result=lambda: SimpleNamespace(values=[SimpleNamespace(string_value=str(tmp_path)), SimpleNamespace(string_value='')]))
    monkeypatch.setattr(rclpy.parameter_client, 'AsyncParameterClient', Parameters)
    def acquire(parameters, active_memory, wait, timeout, acquisition):
        """Mark the fixture freeze acquired and return its authoritative settings."""
        events.append('acquire')
        acquisition['owned'] = True
        return snapshot
    monkeypatch.setattr(smpa, 'acquire_inference', acquire)
    monkeypatch.setattr(smpa, 'wait_for_ros', lambda node, future, timeout, operation: future)
    monkeypatch.setattr(smpa, 'verify_inference', lambda *a: events.append('verify'))
    def invoke(client, goal, wait, timeout, outstanding, **kwargs):
        """Record one Model goal as an immediately completed action."""
        events.append(json.loads(goal.messages_json))
        assert goal.run_id == 'run' and goal.capability == 'belief'
        if unconfirmed:
            outstanding['result'] = object()
            raise TimeoutError('stage still running')
    monkeypatch.setattr(smpa, 'invoke_stage', invoke)
    def drain(outstanding, wait, timeout):
        """Keep ownership when the simulated stage has not finished."""
        events.append('drain')
        if outstanding:
            raise TimeoutError('stage still running')
    monkeypatch.setattr(smpa, 'finish_outstanding_stage', drain)
    monkeypatch.setattr(smpa, 'resolve_inference_acquisition', lambda *a: events.append('resolve'))
    monkeypatch.setattr(smpa, 'release_inference', lambda *a: events.append('release'))
    def exchange(active_memory, message, generate, settings, *, agent, modelling):
        """Simulate the two saved conversation turns and return their reply."""
        modelling(active_memory, [{'role': 'user', 'content': message}])
        modelling(active_memory, [{'role': 'user', 'content': message}, {'role': 'assistant', 'content': 'reply'}])
        return 'reply'
    monkeypatch.setattr(conversation_web, 'exchange', exchange)
    options = {'agent': {'model_id': 'fixture'}, 'settings': {'max_new_tokens': 42}, 'ontology': None}
    if unconfirmed:
        with pytest.raises(TimeoutError, match='stage still running'):
            conversation_web.exchange_with_model(memory, 'hello', Mock(), {}, agent=None, modelling=options)
        assert 'release' not in events and events[-4:] == ['executor_shutdown', 'client_destroy', 'node_destroy', 'shutdown']
        return
    result = conversation_web.exchange_with_model(memory, 'hello', Mock(), {}, agent=None, modelling=options)
    assert result == 'reply'
    assert events[:5] == ['acquire', 'verify', [{'role': 'user', 'content': 'hello'}], 'verify',
                          [{'role': 'user', 'content': 'hello'}, {'role': 'assistant', 'content': 'reply'}]]
    assert events[5:] == ['resolve', 'drain', 'release', 'executor_shutdown', 'client_destroy', 'node_destroy', 'shutdown']
