"""Check experiment inference acquisition and authoritative provenance."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from memory import MemoryManager
from smpa_ros.smpa import acquire_inference, resolve_inference_acquisition, verify_inference


def test_acquisition_persists_exact_server_snapshot(tmp_path):
    """Acquire once, persist the server snapshot and detect later changes.

    Mock parameter responses without a model. The experiment records the exact
    identity, loaded settings, generation settings and server instance it read;
    a different instance must fail verification before another stage starts.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    snapshot = dict(run_id=memory.metadata['run_id'], server_instance_id='server-a', active_model='fixture',
                    loading={'model_id': 'fixture/model'}, generation={'temperature': 0.0})
    parameters = SimpleNamespace(wait_for_services=lambda timeout_sec: True,
        set_parameters_atomically=Mock(return_value=SimpleNamespace(result=SimpleNamespace(successful=True, reason=''))),
        get_parameters=Mock(return_value=SimpleNamespace(values=[SimpleNamespace(string_value=json.dumps(snapshot))])))
    def wait(future, timeout, operation):
        """Return the fake response without starting ROS or inference."""
        return future
    acquisition = {}
    assert acquire_inference(parameters, memory, wait, 1.0, acquisition) == snapshot
    assert acquisition == {'owned': True}
    assert yaml.safe_load((memory.directory / 'metadata.yaml').read_text())['inference'] == snapshot
    verify_inference(parameters, memory, wait, 1.0)
    snapshot['server_instance_id'] = 'server-b'
    parameters.get_parameters.return_value.values[0].string_value = json.dumps(snapshot)
    with pytest.raises(RuntimeError, match='ownership or frozen settings changed'):
        verify_inference(parameters, memory, wait, 1.0)


def test_late_acquisition_must_be_resolved_before_release(tmp_path):
    """Retain a timed-out acquisition until its real response becomes known.

    A timed-out request can still acquire the server. Preserve its future and
    later record success rather than assuming no freeze exists.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    parameters = SimpleNamespace(wait_for_services=lambda timeout_sec: True,
                                 set_parameters_atomically=lambda values: 'pending')
    def timeout(future, limit, operation):
        """Simulate an acquisition response that has not arrived."""
        raise TimeoutError('pending')
    acquisition = {}
    with pytest.raises(TimeoutError, match='pending'):
        acquire_inference(parameters, memory, timeout, 1.0, acquisition)
    assert acquisition == {'future': 'pending'}
    def success(future, limit, operation):
        """Resolve the late request as accepted by the inference server."""
        return SimpleNamespace(result=SimpleNamespace(successful=True, reason=''))
    resolve_inference_acquisition(acquisition, success, 1.0)
    assert acquisition == {'owned': True}
