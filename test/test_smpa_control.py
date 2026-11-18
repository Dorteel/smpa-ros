"""Verify that lifecycle observation delegates to the existing experiment and records."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import rclpy
from rclpy.parameter import Parameter
import yaml
from smpa_ros import run_control
from smpa_ros_interfaces.srv import StartSmpa, StopSmpa, GetSmpaStatus


@pytest.mark.parametrize('failure', [None, 'error', 'interrupt'])
def test_worker_preserves_script_and_persistence(tmp_path, monkeypatch, failure):
    """Relay the direct experiment's actual stage and terminal notifications.

    Use the real action-sequence lifecycle with fake ROS clients and persisted
    run memory. Simulate successful execution, a stage error and an interrupt;
    no model, robot or simulator is started by this test.
    """
    from memory import MemoryManager
    from smpa_ros import smpa as script
    from smpa_ros_interfaces import action
    for name in ('Sense', 'Model', 'Plan', 'Act'):
        monkeypatch.setattr(action, name, SimpleNamespace(Goal=SimpleNamespace), raising=False)
    memory = MemoryManager(tmp_path / 'memory', goal='# Requested goal')
    identity = memory.metadata['run_id']
    snapshot = dict(run_id=identity, server_instance_id='fixture', active_model='fixture', loading={}, generation={})
    stages = []
    def set_parameters(values):
        """Accept the test experiment's ownership changes without a server."""
        return SimpleNamespace(result=SimpleNamespace(successful=True, reason=''))
    parameters = SimpleNamespace(wait_for_services=lambda timeout_sec: True, set_parameters_atomically=set_parameters,
        get_parameters=lambda names: SimpleNamespace(values=[SimpleNamespace(string_value=json.dumps(snapshot))]))
    responses = {'sense': SimpleNamespace(observation='seen', record_path='sense'),
                 'model': SimpleNamespace(world_model='world', record_path='model', snapshot_path='snapshot'),
                 'plan': SimpleNamespace(action_json='{"action":"look_left"}', record_path='plan'),
                 'act': SimpleNamespace(success=True)}
    clients = {}
    for stage in responses:
        def send(goal, stage=stage):
            """Return the selected mock result or the requested Sense failure."""
            stages.append(stage)
            if stage == 'sense' and failure == 'error':
                raise RuntimeError('fixture sense failure')
            if stage == 'sense' and failure == 'interrupt':
                raise KeyboardInterrupt
            return SimpleNamespace(accepted=True, get_result_async=lambda: SimpleNamespace(status=4, result=responses[stage]))
        clients[stage] = SimpleNamespace(wait_for_server=lambda timeout_sec: True, send_goal_async=send)
    def wait(future, timeout, operation):
        """Return completed mocked responses without starting ROS actions."""
        return future
    def experiment(arguments, *, goal, notify):
        """Run the real lifecycle using fake clients and relay its notifications."""
        assert goal == '# Requested goal'
        def relay(stage, event):
            """Attach this run's identity to the lifecycle event."""
            notify(identity, stage, event)
        script.execute_experiment(memory, 1, clients, parameters, wait, 1.0, notify=relay)
    monkeypatch.setattr(script, 'run_ros_experiment', experiment)
    read_descriptor, write_descriptor = os.pipe()
    run_control.run_worker(write_descriptor, '# Requested goal', [])
    with os.fdopen(read_descriptor) as stream:
        events = [json.loads(line) for line in stream]
    metadata = yaml.safe_load((memory.directory / 'metadata.yaml').read_text())
    expected = {'error': 'failed', 'interrupt': 'interrupted'}.get(failure, 'completed')
    assert metadata['status'] == expected
    assert events[0]['event'] == 'run_started' and events[-1]['event'] == 'run_' + expected
    assert all(event['run_id'] == identity for event in events)
    if failure is None:
        assert stages == ['sense', 'model', 'plan', 'act']
        assert metadata['completed_cycles'] == 1
    else:
        assert stages == ['sense'] and metadata['completed_cycles'] == 0
        assert ('stage_failed' in [event['event'] for event in events]) == (failure == 'error')


def test_idle_status_and_rejections(tmp_path):
    """Expose idle status and reject empty goals, duplicate starts and idle stops.

    Exercise the actual service callbacks without spawning a robot process. Fake
    only the running process marker to verify rejection and accepted stop behavior.
    """
    rclpy.init()
    node = run_control.SmpaControl(parameter_overrides=[Parameter('memory_root', value=str(tmp_path))])
    try:
        status = node.status(GetSmpaStatus.Request(), GetSmpaStatus.Response())
        assert (status.state, status.run_id, status.active_stage) == ('idle', '', '')
        assert not node.start(StartSmpa.Request(goal=''), StartSmpa.Response()).accepted
        assert not node.stop(StopSmpa.Request(), StopSmpa.Response()).accepted
        node.state = 'running'
        assert not node.start(StartSmpa.Request(goal='duplicate'), StartSmpa.Response()).accepted
    finally:
        node.destroy_node()
        rclpy.shutdown()


def test_ros_services_start_stop_and_lifecycle(tmp_path, monkeypatch):
    """Drive actual ROS services while an owned fixture child runs the real experiment.

    Substitute only the subprocess entry script to inject safe action clients.
    Verify prompt acceptance, duplicate rejection, stage notifications, graceful
    SIGINT interruption and a subsequent successful run with a distinct identity.
    Match service identities and terminal events against real persisted metadata.
    """
    from concurrent.futures import ThreadPoolExecutor
    from rclpy.executors import MultiThreadedExecutor
    from time import monotonic, sleep
    from smpa_ros_interfaces.msg import SmpaEvent
    real_popen = run_control.subprocess.Popen

    def spawn_fixture(command, **options):
        """Keep the private notification pipe while replacing only the worker entry.

        Execute the fixture with the production descriptor, goal and run arguments.
        No shell is involved and no production module gains a testing switch.
        """
        helper = str(Path(__file__).with_name('tier1_control_worker.py'))
        return real_popen([command[0], helper, *command[4:]], **options)

    monkeypatch.setattr(run_control.subprocess, 'Popen', spawn_fixture)
    rclpy.init()
    node = run_control.SmpaControl(parameter_overrides=[Parameter('memory_root', value=str(tmp_path))])
    client = rclpy.create_node('tier1_control_client')
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    executor.add_node(client)
    pool = ThreadPoolExecutor(max_workers=1)
    spinning = pool.submit(executor.spin)
    events = []

    def receive(message):
        """Collect genuine ROS lifecycle messages for comparison with persistence.

        Keep their observed order; do not infer or synthesize stage transitions.
        """
        events.append(message)

    subscription = client.create_subscription(SmpaEvent, 'smpa/events', receive, 100)
    start = client.create_client(StartSmpa, 'smpa/start')
    stop = client.create_client(StopSmpa, 'smpa/stop')

    def await_future(future):
        """Wait briefly for a ROS reply while the independent executor keeps spinning.

        Fail the test on a missing response instead of silently accepting an
        unavailable service or an experiment that blocks the start callback.
        """
        deadline = monotonic() + 10
        while not future.done() and monotonic() < deadline:
            sleep(0.01)
        assert future.done()
        return future.result()

    try:
        assert start.wait_for_service(timeout_sec=5) and stop.wait_for_service(timeout_sec=5)
        first = await_future(start.call_async(StartSmpa.Request(goal='# WAIT')))
        assert first.accepted and first.run_id
        duplicate = await_future(start.call_async(StartSmpa.Request(goal='# duplicate')))
        assert not duplicate.accepted
        stopped = await_future(stop.call_async(StopSmpa.Request()))
        assert stopped.accepted
        node.reader.join(10)
        assert not node.reader.is_alive() and node.state == 'interrupted'
        second = await_future(start.call_async(StartSmpa.Request(goal='# complete')))
        assert second.accepted and second.run_id != first.run_id
        node.reader.join(10)
        assert not node.reader.is_alive() and node.state == 'completed'
        deadline = monotonic() + 3
        while not any(event.event == 'run_completed' for event in events) and monotonic() < deadline:
            sleep(0.01)
        assert any(event.run_id == first.run_id and event.event == 'run_interrupted' for event in events)
        assert [event.stage for event in events if event.run_id == second.run_id
                and event.event == 'stage_started'] == ['sense', 'model', 'plan', 'act']
        metadata = [yaml.safe_load(path.read_text()) for path in tmp_path.glob('runs/*/metadata.yaml')]
        assert {record['run_id']: record['status'] for record in metadata} == {
            first.run_id: 'interrupted', second.run_id: 'completed'}
    finally:
        if node.process is not None and node.process.poll() is None:
            node.process.send_signal(run_control.signal.SIGINT)
        executor.shutdown(wait_for_threads=True)
        spinning.result(timeout=5)
        pool.shutdown()
        client.destroy_node()
        node.destroy_node()
        rclpy.shutdown()


def test_failed_stop_signal_restores_running_state(tmp_path):
    """Keep a live child marked running when its stop signal cannot be sent.

    Simulate a permission failure without starting a subprocess. The rejected
    stop must not claim that shutdown is in progress or abandon the live child.
    """
    from unittest.mock import Mock
    rclpy.init()
    node = run_control.SmpaControl(parameter_overrides=[Parameter('memory_root', value=str(tmp_path))])
    process = Mock()
    process.poll.return_value = None
    process.send_signal.side_effect = PermissionError('denied')
    node.process, node.state = process, 'running'
    try:
        response = node.stop(StopSmpa.Request(), StopSmpa.Response())
        assert not response.accepted and node.state == 'running'
    finally:
        node.process = None
        node.destroy_node()
        rclpy.shutdown()


def test_child_cleanup_escalates_after_timeouts(tmp_path):
    """Escalate a stuck owned child from interrupt to terminate and then kill.

    Mock process waits so no external program is started. Confirm that each wait
    is bounded and that the child is reaped once the final signal succeeds.
    """
    from unittest.mock import Mock, call
    import subprocess
    rclpy.init()
    node = run_control.SmpaControl(parameter_overrides=[Parameter('memory_root', value=str(tmp_path))])
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired('worker', 1),
                                subprocess.TimeoutExpired('worker', 1), 0]
    try:
        node.stop_child(process)
        process.send_signal.assert_called_once_with(run_control.signal.SIGINT)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        assert process.wait.call_args_list == [call(timeout=run_control.SHUTDOWN_TIMEOUT)] * 3
    finally:
        node.destroy_node()
        rclpy.shutdown()


def test_notification_reader_join_is_bounded(tmp_path):
    """Finish node destruction when the notification reader does not exit.

    Supply a reader that remains alive and confirm the node waits only for the
    configured deadline. No subprocess or actual background thread is started.
    """
    from unittest.mock import Mock
    rclpy.init()
    node = run_control.SmpaControl(parameter_overrides=[Parameter('memory_root', value=str(tmp_path))])
    reader = Mock()
    reader.is_alive.return_value = True
    node.reader = reader
    try:
        node.destroy_node()
        reader.join.assert_called_once_with(run_control.SHUTDOWN_TIMEOUT)
    finally:
        rclpy.shutdown()


def test_startup_timeout_stops_child(tmp_path, monkeypatch):
    """Reject startup and clean up a child that never reports its run identity.

    Replace process creation and notification reading with lightweight mocks.
    Force the startup wait to expire, then verify failed state and bounded child
    cleanup without executing the experiment or touching persistent records.
    """
    from unittest.mock import Mock
    rclpy.init()
    node = run_control.SmpaControl(parameter_overrides=[Parameter('memory_root', value=str(tmp_path))])
    process = Mock()
    process.poll.return_value = None
    process.wait.return_value = 0
    monkeypatch.setattr(run_control.subprocess, 'Popen', lambda *args, **kwargs: process)
    node.start_ready.wait = lambda timeout: False

    def reader(*, target, args, daemon):
        """Close the unused read end while simulating a nonresponsive child.

        The callback is intentionally not started, so no notification can satisfy
        startup. Return an inert reader that does not block later destruction.
        """
        os.close(args[0])
        thread = Mock()
        thread.is_alive.return_value = False
        return thread

    monkeypatch.setattr(run_control, 'Thread', reader)
    try:
        result = node.start(StartSmpa.Request(goal='goal'), StartSmpa.Response())
        assert not result.accepted and node.state == 'failed'
        process.send_signal.assert_called_once_with(run_control.signal.SIGINT)
        process.wait.assert_called_once_with(timeout=run_control.SHUTDOWN_TIMEOUT)
    finally:
        node.process = None
        node.destroy_node()
        rclpy.shutdown()


@pytest.mark.parametrize('notification', ['', 'not-json\n'])
def test_missing_or_invalid_notification_marks_failure(tmp_path, notification):
    """Report a child failure when its notification stream ends without a result.

    Feed an empty or malformed private pipe to the real reader while using an
    already exited mock process. Startup waiters must be released and the node
    must report failure instead of remaining in its running state.
    """
    from unittest.mock import Mock
    rclpy.init()
    node = run_control.SmpaControl(parameter_overrides=[Parameter('memory_root', value=str(tmp_path))])
    node.state = 'running'
    process = Mock()
    process.poll.return_value = 1
    read_descriptor, write_descriptor = os.pipe()
    try:
        os.write(write_descriptor, notification.encode())
        os.close(write_descriptor)
        node.collect(read_descriptor, process)
        assert node.state == 'failed'
        assert node.start_ready.is_set()
    finally:
        node.destroy_node()
        rclpy.shutdown()


def test_stop_monitor_reports_missing_terminal_notification(tmp_path, monkeypatch):
    """Fail a stopping run if the reader cannot report its terminal outcome.

    Simulate an exited child and a reader that remains blocked after its bounded
    join. The status must not remain stopping or claim persisted interruption.
    """
    from unittest.mock import Mock
    rclpy.init()
    node = run_control.SmpaControl(parameter_overrides=[Parameter('memory_root', value=str(tmp_path))])
    process, reader = Mock(), Mock()
    process.poll.return_value = 0
    reader.is_alive.return_value = True
    node.state, node.run_id = 'stopping', 'run-1'
    try:
        node.monitor_stop(process, reader)
        assert node.state == 'failed' and node.terminal_seen
        reader.join.assert_called_once_with(run_control.SHUTDOWN_TIMEOUT)
    finally:
        node.destroy_node()
        rclpy.shutdown()


def test_start_rejects_failed_freeze_acquisition_with_run_identity(tmp_path, monkeypatch):
    """Reject startup when a child fails before confirming its inference freeze.

    A failed child may already have persisted a run ID. Relay that failure over
    the real notification pipe and verify StartSmpa does not claim the run was
    accepted merely because the identity exists.
    """
    rclpy.init()
    node = run_control.SmpaControl(parameter_overrides=[Parameter('memory_root', value=str(tmp_path))])
    process = SimpleNamespace(poll=lambda: 0, wait=lambda timeout=None: 0)
    def launch(command, *, pass_fds, start_new_session):
        """Write a failed startup notification without starting a real process."""
        record = dict(run_id='failed-run', stage='', event='run_failed')
        os.write(pass_fds[0], (json.dumps(record) + '\n').encode())
        return process
    monkeypatch.setattr(run_control.subprocess, 'Popen', launch)
    try:
        response = node.start(StartSmpa.Request(goal='test'), StartSmpa.Response())
        assert not response.accepted and response.run_id == ''
        assert node.state == 'failed' and node.run_id == 'failed-run'
    finally:
        node.destroy_node()
        rclpy.shutdown()
