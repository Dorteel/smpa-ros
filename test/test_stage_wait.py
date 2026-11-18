"""Check bounded stage requests and retention of unconfirmed operations."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from smpa_ros.smpa import invoke_stage, finish_outstanding_stage


def test_stage_result_timeout_keeps_ownership_until_terminal():
    """Keep an accepted stage outstanding until its terminal result arrives.

    The initial result wait times out. Cleanup later sees the same result future,
    confirms termination and clears the outstanding state without resending it.
    """
    handle = SimpleNamespace(accepted=True, get_result_async=Mock(return_value='result'))
    client = SimpleNamespace(wait_for_server=lambda timeout_sec: True, send_goal_async=lambda goal: 'acceptance')
    outstanding = {}
    def wait(future, timeout, operation):
        """Accept the goal and simulate a timeout on its first result wait."""
        if future == 'acceptance':
            return handle
        raise TimeoutError('result unavailable')
    with pytest.raises(TimeoutError, match='result unavailable'):
        invoke_stage(client, object(), wait, 1.0, outstanding)
    assert outstanding['handle'] is handle and outstanding['result'] == 'result'
    with pytest.raises(RuntimeError, match='earlier stage'):
        invoke_stage(client, object(), wait, 1.0, outstanding)
    def terminal(future, timeout, operation):
        """Confirm completion of the previously outstanding result future."""
        assert future == 'result'
        return SimpleNamespace(status=4)
    finish_outstanding_stage(outstanding, terminal, 1.0)
    assert not outstanding and handle.get_result_async.call_count == 1


def test_late_acceptance_is_drained_before_cleanup():
    """Resolve late goal acceptance before declaring a stage finished.

    A timed-out send may still be accepted remotely. Cleanup must obtain that
    handle, wait for its terminal result, and retain the state if waiting fails.
    """
    handle = SimpleNamespace(accepted=True, get_result_async=Mock(return_value='result'))
    outstanding = {'acceptance': 'acceptance'}
    def wait(future, timeout, operation):
        """Return the late handle and then simulate another timeout."""
        if future == 'acceptance':
            return handle
        raise TimeoutError('still running')
    with pytest.raises(TimeoutError, match='still running'):
        finish_outstanding_stage(outstanding, wait, 1.0)
    assert outstanding['handle'] is handle and outstanding['result'] == 'result'


def test_ros_wait_returns_result_and_reports_timeout(monkeypatch):
    """Bound ROS waiting and preserve the actual future outcome.

    A completed future returns its response. An incomplete future raises an
    operation-specific timeout, with no assumption that the remote work stopped.
    """
    import rclpy
    from smpa_ros.smpa import wait_for_ros
    monkeypatch.setattr(rclpy, 'spin_until_future_complete', lambda node, future, timeout_sec: None)
    ready = SimpleNamespace(done=lambda: True, result=lambda: 'response')
    pending = SimpleNamespace(done=lambda: False)
    assert wait_for_ros(object(), ready, 1.0, 'fixture') == 'response'
    with pytest.raises(TimeoutError, match='fixture'):
        wait_for_ros(object(), pending, 1.0, 'fixture')


def test_release_requires_no_outstanding_stage_and_names_owner():
    """Release only a confirmed-idle experiment using its owner-checked command.

    An unconfirmed stage prevents any release request. Once it has ended, the
    exact run identity must be sent to the server and unsuccessful replies must
    remain visible to the caller.
    """
    from smpa_ros.smpa import release_inference
    parameters = SimpleNamespace(set_parameters_atomically=Mock(return_value='future'))
    def success(future, timeout, operation):
        """Return an accepted release response for the sent request."""
        return SimpleNamespace(result=SimpleNamespace(successful=True, reason=''))
    with pytest.raises(RuntimeError, match='unconfirmed'):
        release_inference(parameters, 'run-a', success, 1.0, {'result': object()})
    parameters.set_parameters_atomically.assert_not_called()
    release_inference(parameters, 'run-a', success, 1.0, {})
    assert parameters.set_parameters_atomically.call_args.args[0][0].value == 'release:run-a'
    def rejected(future, timeout, operation):
        """Return a failed release without pretending the freeze was cleared."""
        return SimpleNamespace(result=SimpleNamespace(successful=False, reason='server busy'))
    with pytest.raises(RuntimeError, match='server busy'):
        release_inference(parameters, 'run-a', rejected, 1.0, {})
