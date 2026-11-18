"""Edit this program to choose goals, prompts, repetitions, and stage order."""
from functools import partial
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
    """Run the explicitly scripted cognitive actions from the command line.

    Resolve the existing experiment configuration and hand it to the direct ROS
    action execution path. The four stage servers and inference server are
    independent participants and must already be running.
    """
    run_ros_experiment(args=args)


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


def acquire_inference(parameters, memory, wait, timeout, acquisition):
    """Acquire this run's inference freeze and persist the server's snapshot.

    Send the run identity to the inference server and retain the pending future
    until its outcome is known. After acceptance, read the server-captured JSON,
    verify its identity and required fields, and write those exact settings into
    run metadata before any cognitive inference begins. An existing snapshot
    must match, including the server instance, so a restarted or replaced server
    cannot silently continue an earlier experiment. On failure the caller owns
    cleanup; an unconfirmed acquisition must be resolved before release.
    """
    import json
    from rclpy.parameter import Parameter
    if acquisition:
        raise RuntimeError('Inference acquisition is already pending')
    if not parameters.wait_for_services(timeout_sec=timeout):
        raise TimeoutError('Inference parameter services are unavailable')
    identity = memory.metadata['run_id']
    future = parameters.set_parameters_atomically([Parameter('experiment_run_id', value=identity)])
    acquisition['future'] = future
    response = wait(future, timeout, 'inference acquisition')
    acquisition.clear()
    if not response.result.successful:
        raise RuntimeError(f'Inference acquisition failed: {response.result.reason}')
    acquisition['owned'] = True
    response = wait(parameters.get_parameters(['frozen_settings_json']), timeout, 'frozen inference settings')
    snapshot = json.loads(response.values[0].string_value)
    if (not isinstance(snapshot, dict) or snapshot.get('run_id') != identity
            or not snapshot.get('server_instance_id') or not snapshot.get('active_model')
            or not isinstance(snapshot.get('loading'), dict) or not isinstance(snapshot.get('generation'), dict)):
        raise ValueError('Inference server returned an invalid frozen settings snapshot')
    models = memory.metadata.get('models', {})
    expected = {models[stage].get('model_id') for stage in ('vision', 'model', 'plan')
                if isinstance(models.get(stage), dict) and models[stage].get('model_id')}
    if expected and expected != {snapshot['loading'].get('model_id')}:
        raise RuntimeError('Loaded inference model does not match the experiment configuration')
    existing = memory.metadata.get('inference')
    if existing is not None and existing != snapshot:
        raise RuntimeError('Inference server or frozen settings changed during this experiment')
    memory.metadata['inference'] = snapshot
    memory.write_metadata()
    return snapshot


def resolve_inference_acquisition(acquisition, wait, timeout):
    """Resolve a timed-out freeze request before attempting experiment cleanup.

    A timed-out parameter request may still acquire the server. Wait for its
    actual response and remember whether it succeeded. If its outcome remains
    unknown, propagate the error and keep the future so cleanup cannot release
    prematurely or claim that the server is free.
    """
    if 'future' not in acquisition:
        return
    response = wait(acquisition['future'], timeout, 'late inference acquisition')
    acquisition.clear()
    acquisition['owned'] = response.result.successful


def verify_inference(parameters, memory, wait, timeout):
    """Verify the server still owns this run with the recorded frozen settings.

    Read the authoritative server snapshot and compare it to the snapshot saved
    in run metadata. Missing ownership, changed settings, or a new server instance
    abort the experiment before it sends another cognitive action.
    """
    import json
    response = wait(parameters.get_parameters(['frozen_settings_json']), timeout, 'inference verification')
    value = response.values[0].string_value
    if not value or json.loads(value) != memory.metadata.get('inference'):
        raise RuntimeError('Inference server ownership or frozen settings changed')


def execute_experiment(memory, cycles, clients, parameters, wait, timeout, *, notify=None, cleanup=None):
    """Run the explicit cognitive sequence under one recorded inference freeze.

    The caller creates the run, ROS clients and bounded wait function. Acquire
    and persist the server's frozen settings before announcing or sending any
    stage action. Before each stage verify that the same server and settings
    remain owned, then send the requested action through its existing client.

    On completion or failure, resolve any late acquisition and wait briefly for
    a previously accepted stage to terminate. Release the freeze only when both
    outcomes are known; otherwise leave it in place for safe recovery. Limit
    each cleanup wait to three seconds. Finalize
    run metadata and notify the lifecycle observer of the actual terminal state.
    Propagate the original error, or a cleanup error when cleanup is incomplete.
    """
    acquisition = {}
    outstanding = {}
    status = 'completed'
    failure = None
    cleanup_error = None
    try:
        acquire_inference(parameters, memory, wait, timeout, acquisition)
        if notify is not None:
            notify('', 'run_started')
        def invoke(stage, action_type, goal):
            """Verify ownership and invoke the stage selected by the script.

            The stage name selects its already-created ROS client. Reject a
            changed or restarted inference server before sending the goal, then
            retain unconfirmed operations for the enclosing cleanup.
            """
            verify_inference(parameters, memory, wait, timeout)
            return invoke_stage(clients[stage], goal, wait, timeout, outstanding)
        run_actions(memory, cycles, invoke, notify=notify)
    except KeyboardInterrupt as error:
        status, failure = 'interrupted', error
    except BaseException as error:
        status, failure = 'failed', error
    try:
        cleanup_timeout = min(timeout, 3.0)
        resolve_inference_acquisition(acquisition, wait, cleanup_timeout)
        finish_outstanding_stage(outstanding, wait, cleanup_timeout)
        if acquisition.get('owned'):
            release_inference(parameters, memory.metadata['run_id'], wait, cleanup_timeout, outstanding)
    except BaseException as error:
        status, cleanup_error = 'failed', error
    if cleanup is not None:
        try:
            cleanup()
        except BaseException as error:
            status, cleanup_error = 'failed', cleanup_error or error
    try:
        memory.finalize(status)
    except BaseException as error:
        status, cleanup_error = 'failed', cleanup_error or error
    if notify is not None:
        event = {'completed': 'run_completed', 'interrupted': 'run_interrupted'}.get(status, 'run_failed')
        notify('', event)
    if cleanup_error is not None:
        raise RuntimeError(f'Experiment cleanup was not confirmed: {cleanup_error}') from failure or cleanup_error
    if failure is not None:
        raise failure


def prepare_experiment(args, goal):
    """Resolve the existing experiment settings without loading any model.

    Reuse the established configuration and model-catalogue resolution while
    the remaining Runner responsibilities are migrated. Construct the same
    frozen run configuration and metadata as the legacy entry point, then
    create a new run. Resolving configuration does not create a backend or start
    a stage; the shared inference server owns its already-loaded model.
    """
    from copy import deepcopy
    from types import SimpleNamespace
    from memory import MemoryManager
    from smpa_ros.plan import ACTION_SCHEMA, enabled_actions, refinement_settings
    from smpa_ros.model import validate_settings as validate_model_settings
    from smpa_ros.plan import validate_settings as validate_plan_settings

    profile = SimpleNamespace()
    Runner.configure(profile, args)
    Runner.initialize_backends(profile)
    if isinstance(profile.limit, bool) or not isinstance(profile.limit, int) or profile.limit < 1:
        raise ValueError('max_cycles must be a positive integer')
    refinement_settings(profile.config)
    validate_model_settings(profile.config.get('modeling', {}))
    validate_plan_settings(profile.config.get('planning', {}))
    configuration = dict(pipeline=profile.config, service=profile.options.service, demo=profile.options.demo, max_cycles=profile.limit,
        available_actions=deepcopy(enabled_actions(profile.config)),
        output_schema=deepcopy(profile.config['stage_settings']['plan'].get('output_schema', ACTION_SCHEMA)))
    configuration['stage_settings'] = deepcopy(profile.config['stage_settings'])
    configuration['stage_models'] = {
        'sense': deepcopy(profile.models.get('vision', {})),
        'model': deepcopy(profile.models.get('model', {})),
        'plan': deepcopy(profile.models.get('plan', profile.models.get('model', {}))),
        'act': dict(backend='ros_action', model_id=None, provider=None)}
    for stage, model in configuration['stage_models'].items():
        model.setdefault('provider', None)
        if stage == 'sense':
            model.setdefault('backend', profile.config.get('vision', {}).get('backend', 'transformers'))
    if 'stage_prompts' not in profile.original_config:
        configuration['stage_prompts'] = profile.config['stage_prompts']
    memory = MemoryManager(profile.options.world_model_root, profile.options.run_name,
                           models=profile.models, configuration=configuration, goal=goal)
    memory.metadata['max_cycles'] = profile.limit
    memory.write_metadata()
    return profile, memory


def run_ros_experiment(args=None, *, goal=GOAL, notify=None):
    """Run the experiment through independent stage and inference ROS servers.

    Create a run using existing configuration rules, optionally launch its
    established speech participants, and construct direct clients for all four
    stage actions. The script acquires and records inference ownership, invokes
    its explicit sequence, and cleans up the owned clients and speech process.
    The optional observer receives the run identity and lifecycle events. No
    cognitive backend or other stage is loaded in this process.
    """
    import os
    import signal
    import subprocess
    import rclpy
    from rclpy.action import ActionClient
    from rclpy.node import Node
    from rclpy.parameter_client import AsyncParameterClient
    from rclpy.signals import SignalHandlerOptions
    from smpa_ros_interfaces.action import Sense, Model, Plan, Act

    profile, memory = prepare_experiment(args, goal)
    identity = memory.metadata['run_id']
    participant = None
    node = None
    clients = {}
    initialized = False
    announced_terminal = False
    closed = False
    def publish(stage, event):
        """Relay one observed lifecycle event with this persisted run identity."""
        nonlocal announced_terminal
        if event in ('run_completed', 'run_interrupted', 'run_failed'):
            announced_terminal = True
        if notify is not None:
            notify(identity, stage, event)
    def stop_participant():
        """Stop only the speech process group launched for this experiment.

        Request graceful interruption first, then terminate or kill only that
        owned process if its bounded waits expire. An already exited participant
        needs no further action.
        """
        if participant is None or participant.poll() is not None:
            return
        for signum, timeout in ((signal.SIGINT, 2), (signal.SIGTERM, 2), (signal.SIGKILL, 2)):
            os.killpg(participant.pid, signum)
            try:
                participant.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                continue
        raise RuntimeError('Speech participant did not terminate')
    def cleanup_owned_resources():
        """Close every resource owned by this run and report cleanup failures.

        Stop the speech child, destroy all stage clients and the experiment node,
        and shut down the ROS context even when an earlier cleanup step fails.
        Retain the first error so the run cannot be finalized as successful.
        """
        nonlocal closed
        if closed:
            return
        closed = True
        errors = []
        try:
            stop_participant()
        except BaseException as error:
            errors.append(error)
        for client in clients.values():
            try:
                client.destroy()
            except BaseException as error:
                errors.append(error)
        if node is not None:
            try:
                node.destroy_node()
            except BaseException as error:
                errors.append(error)
        if initialized:
            try:
                if rclpy.ok():
                    rclpy.shutdown()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise RuntimeError(f'Owned resource cleanup failed: {errors[0]}') from errors[0]
    try:
        if profile.modalities['hear']:
            command = ['ros2', 'launch', 'smpa_ros', 'llm.launch.py', 'participants_only:=true',
                       'service:=speech', f'world_model_dir:={memory.directory}', f'run_id:={identity}',
                       f'config:={profile.options.config.resolve()}', f'python:={profile.options.python}',
                       f'model_root:={profile.options.model_root}']
            for name in ('audio_lock', 'input_device', 'output_device'):
                value = getattr(profile.options, name)
                if value:
                    command.append(f'{name}:={value}')
            participant = subprocess.Popen(command, start_new_session=True)
        rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
        initialized = True
        node = Node('smpa_experiment')
        for stage, kind in (('sense', Sense), ('model', Model), ('plan', Plan), ('act', Act)):
            clients[stage] = ActionClient(node, kind, '/smpa/' + stage)
        parameters = AsyncParameterClient(node, '/llm')
        wait = partial(wait_for_ros, node)
        execute_experiment(memory, profile.limit, clients, parameters, wait, profile.options.timeout,
                           notify=publish, cleanup=cleanup_owned_resources)
    except BaseException as error:
        status = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
        cleanup_error = None
        try:
            cleanup_owned_resources()
        except BaseException as cleanup_failure:
            status, cleanup_error = 'failed', cleanup_failure
        if memory.metadata['status'] == 'running':
            memory.finalize(status)
        if not announced_terminal:
            publish('', 'run_interrupted' if status == 'interrupted' else 'run_failed')
        if cleanup_error is not None:
            raise RuntimeError(f'Experiment cleanup was not confirmed: {cleanup_error}') from error
        raise
