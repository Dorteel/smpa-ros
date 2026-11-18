"""Edit this program to choose goals, prompts, repetitions, and stage order."""
from runner import Runner

GOAL = '# Goal\n\nExplore the environment to discover previously unobserved objects and relationships.\n'


def run(runner, cycles, *, goal=GOAL):
    for experiment in range(1):
        runner.start(goal=goal)
        for cycle in range(cycles):
            with runner.cycle():
                observation = runner.sense(prompt=None)
                world = runner.model(observation, prompt='default')
                action = runner.plan(world, prompt='default')
                runner.act(action, prompt='default')
            if runner.experiment_complete:
                break
        runner.finish()


def main(args=None):
    with Runner(args) as runner:
        run(runner, runner.limit)


def run_actions(memory, cycles, invoke, *, notify=None):
    """Execute the experiment's explicit Sense, Model, Plan and Act sequence.

    The experiment owns the active run and cycle boundaries. The supplied
    invocation function sends an action goal and returns its successful result;
    it does not decide stage order. Carry each stage's persisted source record
    to the next and carry the latest world-model snapshot into the next cycle.
    Notify the lifecycle observer around each request when one is supplied.

    Complete a cycle only after Act succeeds. If a request fails, report the
    failed stage, leave the cycle incomplete and propagate the error. Refinement
    can terminate the loop when its persisted state becomes complete. Run setup,
    inference ownership, and finalization remain the caller's responsibility.
    """
    import json
    from smpa_ros_interfaces.action import Sense, Model, Plan, Act

    def request(stage, action_type, goal):
        """Invoke the selected stage once and publish its actual outcome.

        Notify immediately before sending the request, report a failure if the
        invocation raises, and publish completion only after a successful result.
        The supplied action client is responsible for bounded waits and cleanup.
        """
        if notify is not None:
            notify(stage, 'stage_started')
        try:
            result = invoke(stage, action_type, goal)
            if stage == 'act' and not result.success:
                raise RuntimeError('Act did not complete successfully')
        except Exception:
            if notify is not None:
                notify(stage, 'stage_failed')
            raise
        if notify is not None:
            notify(stage, 'stage_completed')
        return result

    run_id = memory.metadata['run_id']
    previous_snapshot = ''
    for cycle in range(cycles):
        memory.begin_cycle()
        try:
            sensed = request('sense', Sense, Sense.Goal(run_id=run_id, capability=''))
            modeled = request('model', Model, Model.Goal(
                run_id=run_id, capability='', observation=sensed.observation,
                source_record=sensed.record_path, previous_snapshot=previous_snapshot))
            previous_snapshot = modeled.snapshot_path
            planned = request('plan', Plan, Plan.Goal(
                run_id=run_id, capability='', world_model=modeled.world_model,
                source_record=modeled.record_path))
            acted = request('act', Act, Act.Goal(
                run_id=run_id, capability='', action_json=planned.action_json,
                source_record=planned.record_path))
            memory.complete_cycle()
        finally:
            memory.in_cycle = False
        refinement = memory.directory / 'state' / 'refinement.json'
        if refinement.exists() and json.loads(refinement.read_text()).get('status') == 'completed':
            break


def invoke_stage(client, goal, wait, timeout, outstanding):
    """Send one stage goal and retain any operation whose end is unconfirmed.

    The client owns a single cognitive action endpoint. Wait for its server,
    acceptance and terminal result through the supplied bounded wait function.
    Keep the acceptance or result future in the shared outstanding dictionary
    until the remote goal is known to have terminated. This lets experiment
    cleanup retain inference ownership if a timeout leaves a stage still active.
    Return the result only for a successful ROS action; propagate all failures.
    """
    from action_msgs.msg import GoalStatus
    if outstanding:
        raise RuntimeError('An earlier stage action has not reached a terminal state')
    if not client.wait_for_server(timeout_sec=timeout):
        raise TimeoutError('Cognitive stage action server is unavailable')
    acceptance = client.send_goal_async(goal)
    outstanding['acceptance'] = acceptance
    handle = wait(acceptance, timeout, 'stage acceptance')
    if not handle.accepted:
        outstanding.clear()
        raise RuntimeError('Cognitive stage action goal was rejected')
    outstanding['handle'] = handle
    result_future = handle.get_result_async()
    outstanding['result'] = result_future
    response = wait(result_future, timeout, 'stage result')
    outstanding.clear()
    if response.status != GoalStatus.STATUS_SUCCEEDED:
        raise RuntimeError(f'Cognitive stage action failed with status {response.status}')
    return response.result


def finish_outstanding_stage(outstanding, wait, timeout):
    """Confirm an interrupted stage has ended before releasing shared inference.

    An acceptance response may have arrived after the original timeout. Resolve
    it first; a rejected goal is already terminal. For an accepted goal wait for
    its terminal result, using the existing result future when available. Clear
    the outstanding state only after termination is confirmed. Propagate a
    timeout or transport error so the caller keeps the inference freeze in place.
    """
    if not outstanding:
        return
    handle = outstanding.get('handle')
    if handle is None:
        handle = wait(outstanding['acceptance'], timeout, 'late stage acceptance')
        if not handle.accepted:
            outstanding.clear()
            return
        outstanding['handle'] = handle
    if 'result' not in outstanding:
        outstanding['result'] = handle.get_result_async()
    wait(outstanding['result'], timeout, 'terminal stage result')
    outstanding.clear()


def wait_for_ros(node, future, timeout, operation):
    """Wait for one ROS response without blocking the experiment indefinitely.

    Spin the experiment's own node for at most the requested interval. Return
    the response or propagate its exception when the future completes. If it
    does not complete, raise a named timeout without assuming the remote action
    or parameter request has stopped.
    """
    import rclpy
    if timeout <= 0:
        raise ValueError('ROS operation timeout must be positive')
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
    if not future.done():
        raise TimeoutError(f'Timed out waiting for {operation}')
    return future.result()


def release_inference(parameters, run_id, wait, timeout, outstanding):
    """Release only this experiment's inference freeze after its stages finish.

    Refuse to release while a cognitive action or freeze acquisition remains
    unconfirmed. Send the server's owner-checked release command and require a
    successful response. A rejection or timeout is reported to the caller; it
    must not claim cleanup succeeded or clear another experiment's ownership.
    """
    from rclpy.parameter import Parameter
    if outstanding:
        raise RuntimeError('Cannot release inference while an operation is unconfirmed')
    response = wait(parameters.set_parameters_atomically([
        Parameter('experiment_run_id', value='release:' + run_id)]), timeout, 'inference release')
    if not response.result.successful:
        raise RuntimeError(f'Inference release failed: {response.result.reason}')
