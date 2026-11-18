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
    """Observe the actual experiment with injected safe stages and failure cases.

    Use the real Runner and smpa.run function, replacing only sensory/inference/
    action callbacks. Assert normal stage ordering and terminal metadata, and
    require interruption/failure to retain the original persisted semantics.
    """
    import runner as runner_module
    original_runner = runner_module.Runner
    stages = []

    def sense(directory, cycle):
        """Record fixture acquisition and deliberately raise selected failure modes.

        Normal acquisition supplies a fixed observation. Error and interruption
        cases test the existing Runner context's cleanup and metadata contracts.
        """
        stages.append('sense')
        if failure == 'error':
            raise RuntimeError('fixture sense failure')
        if failure == 'interrupt':
            raise KeyboardInterrupt
        return 'fixture observation'

    def model(prompt, **settings):
        """Record Model execution and return one Markdown world snapshot.

        This avoids loading weights while retaining the actual Model persistence.
        """
        stages.append('model')
        return '# World Model\n\nFixture evidence.'

    def plan(prompt, **settings):
        """Record planning and return a validated fixture action selection.

        The corresponding action callback is injected, so no ROS movement occurs.
        """
        stages.append('plan')
        return '{"action":"look_left"}'

    def act(action, record):
        """Record the safe injected action outcome without contacting any robot.

        Set the same success fields used by existing Runner tests and persistence.
        """
        stages.append('act')
        record.update(goal_accepted=True, result_status=4, success=True)

    def make_runner(arguments):
        """Supply the ordinary Runner with fixture callbacks and a temporary run root.

        The adapter must call the existing experiment, rather than replacing these
        callbacks with its own loop. The request goal still reaches MemoryManager.
        """
        return original_runner(root=tmp_path / 'memory', sense=sense, model=model, plan=plan, act=act)

    monkeypatch.setattr(runner_module, 'Runner', make_runner)
    read_descriptor, write_descriptor = os.pipe()
    run_control.run_worker(write_descriptor, '# Requested goal', [])
    with os.fdopen(read_descriptor) as stream:
        events = [json.loads(line) for line in stream]
    directory = next((tmp_path / 'memory/runs').iterdir())
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert (directory / 'goal.md').read_text() == '# Requested goal'
    expected = {'error': 'failed', 'interrupt': 'interrupted'}.get(failure, 'completed')
    assert metadata['status'] == expected
    assert events[0]['event'] == 'run_started'
    assert all(event['run_id'] == metadata['run_id'] for event in events)
    assert events[-1]['event'] == 'run_' + expected
    if failure is None:
        assert stages == ['sense', 'model', 'plan', 'act']
        assert [(event['stage'], event['event']) for event in events[1:-1]] == [
            (stage, event) for stage in stages for event in ('stage_started', 'stage_completed')]
        assert metadata['completed_cycles'] == 1
    else:
        assert stages == ['sense']
        assert metadata['completed_cycles'] == 0
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

    Substitute only the subprocess entry script to inject safe stage callbacks.
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

        Execute the fixture with the production descriptor, goal and Runner args.
        No shell is involved and no production module gains a testing switch.
        """
        helper = str(Path(__file__).with_name('tier1_control_worker.py'))
        return real_popen([command[0], helper, *command[4:]], **options)

    monkeypatch.setattr(run_control.subprocess, 'Popen', spawn_fixture)
    rclpy.init()
    node = run_control.SmpaControl(parameter_overrides=[Parameter('memory_root', value=str(tmp_path))])
    client = rclpy.create_node('tier1_control_client')
    executor = MultiThreadedExecutor(num_threads=4)
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
