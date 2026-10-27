"""Own one run and execute sequential Sense–Model–Plan–Act cycles."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from importlib.resources import files
from pathlib import Path
from time import perf_counter

import os
import re
import signal
import subprocess
import sys
from tempfile import NamedTemporaryFile
from uuid import uuid4
import yaml

from smpa_ros.backends.base import Backend

DEFAULT_GOAL = '# Goal\n\nExplore the environment to discover previously unobserved objects and relationships.\n'
AVAILABLE_ACTIONS = {
    'look_left': {'endpoint': '/look_left', 'type': 'simulation_actions/action/Look'},
    'look_right': {'endpoint': '/look_right', 'type': 'simulation_actions/action/Look'},
}
ACTION_SCHEMA = {'type': 'object', 'properties': {'action': {'type': 'string',
                 'enum': list(AVAILABLE_ACTIONS)}}, 'required': ['action'], 'additionalProperties': False}


def assigned_run(directory, run_id=''):
    if not directory:
        raise ValueError('Missing world_model_dir: launch llm.launch.py or supply an existing run directory')
    path = Path(directory)
    if not path.is_absolute():
        raise ValueError('world_model_dir must be an absolute existing run directory')
    try:
        metadata = yaml.safe_load((path / 'metadata.yaml').read_text())
        identity = metadata['run_id']
        if not identity or (run_id and identity != run_id):
            raise ValueError('run_id does not match assigned metadata')
        if any(not (path / name).is_dir() for name in ('observations/vision', 'observations/audio', 'world_model', 'state')):
            raise ValueError('Assigned run is missing observation directories')
    except (OSError, KeyError, TypeError) as error:
        raise ValueError(f'Invalid assigned world-model run: {path}') from error
    return path, identity


def write_metadata(directory, metadata):
    with NamedTemporaryFile(mode='w', dir=directory, prefix='.metadata-', delete=False) as output:
        pending = Path(output.name)
        try:
            yaml.safe_dump(metadata, output, sort_keys=False)
            output.flush()
            os.fsync(output.fileno())
            pending.replace(directory / 'metadata.yaml')
        finally:
            pending.unlink(missing_ok=True)


def initialize_run(root, run_name='smpa', *, models=None, configuration=None, goal=DEFAULT_GOAL):
    name = re.sub(r'[^a-zA-Z0-9_-]+', '_', run_name).strip('_-')[:80]
    if not name:
        raise ValueError('run_name must contain a letter, digit, underscore, or hyphen')
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError('Run goal must be nonempty Markdown')
    identity = str(uuid4())
    started = datetime.now().astimezone()
    directory = Path(root).resolve() / 'runs' / f'{started:%Y-%m-%d_%H%M%S_%f%z}_{name}_{identity}'
    directory.mkdir(parents=True, exist_ok=False)
    for category in ('observations/vision', 'observations/audio', 'world_model', 'state'):
        (directory / category).mkdir(parents=True)
    with (directory / 'goal.md').open('x', encoding='utf-8') as output:
        output.write(goal)
    metadata = dict(run_id=identity, run_name=name, started_at=started.isoformat(),
                    completed_at=None, status='running', completed_cycles=0,
                    models=models or {}, configuration=configuration or {})
    write_metadata(directory, metadata)
    return directory, metadata


def finalize_run(directory, metadata, status):
    if status not in ('completed', 'interrupted', 'failed'):
        raise ValueError(f'Invalid terminal run status: {status}')
    if metadata['status'] == 'running':
        metadata.update(status=status, completed_at=datetime.now().astimezone().isoformat())
        write_metadata(directory, metadata)


def stage_prompt(name, value, previous_world_model='', goal='', available_actions=''):
    component = files('smpa_ros').joinpath(name)
    prompt = component.joinpath('prompts/default.txt').read_text(encoding='utf-8').format(input=value, previous_world_model=previous_world_model, goal=goal, available_actions=available_actions)
    settings = json.loads(component.joinpath('models/default.json').read_text(encoding='utf-8'))
    return prompt, settings


def run(max_cycles=1, *, root=None, run_name='smpa', sense,
        model=None, plan=None, act=None, models=None, configuration=None,
        setup=None, teardown=None, model_identifier=None, plan_identifier=None, goal=DEFAULT_GOAL):
    """Own the entire session. Each sense callback receives its run and cycle number.

    Model creates a snapshot; Plan chooses one constrained action; Act waits for its result.
    Results are committed after each stage; the count advances only after Act succeeds.
    """
    if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1:
        raise ValueError('max_cycles must be a positive integer')
    pipeline = (configuration or {}).get('pipeline', {})
    for stage in ('modeling', 'planning'):
        effort = pipeline.get(stage, {}).get('reasoning_effort')
        if effort is not None and effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh'):
            raise ValueError(f'{stage}.reasoning_effort is unsupported')
        if 'max_output_tokens' in pipeline.get(stage, {}):
            limit = pipeline[stage]['max_output_tokens']
            if type(limit) is not int or limit <= 0:
                raise ValueError(f'{stage}.max_output_tokens must be a positive integer')
    directory, metadata = initialize_run(root or Path.cwd() / 'memory', run_name,
                                         models=models, configuration=configuration, goal=goal)
    metadata['max_cycles'] = max_cycles
    status = 'failed'
    try:
        write_metadata(directory, metadata)
        if setup:
            setup(directory, metadata['run_id'])
        for cycle in range(1, max_cycles + 1):
            destination = directory / 'state' / f'cycle_{cycle:06d}'
            destination.mkdir()
            prior_files = set((directory / 'observations').rglob('*'))
            value = sense(directory, cycle)
            with (destination / 'sense.json').open('x', encoding='utf-8') as output:
                json.dump({'stage': 'sense', 'cycle': cycle, 'output': value}, output, ensure_ascii=False, indent=2)
            observations = [str(path.relative_to(directory)) for path in sorted(
                set((directory / 'observations').rglob('*')) - prior_files) if path.is_file()]
            source_record = str((destination / 'sense.json').relative_to(directory))
            previous = directory / 'world_model' / f'iteration_{cycle - 1:03d}.md' if cycle > 1 else None
            previous_text = previous.read_text(encoding='utf-8') if previous is not None else ''
            prompt, settings = stage_prompt('model', value, previous_text)
            if 'max_output_tokens' in pipeline.get('modeling', {}):
                settings['max_new_tokens'] = pipeline['modeling']['max_output_tokens']
            if pipeline.get('modeling', {}).get('reasoning_effort') is not None:
                settings['reasoning_effort'] = pipeline['modeling']['reasoning_effort']
            snapshot = directory / 'world_model' / f'iteration_{cycle:03d}.md'
            backend_id = getattr(model, 'model_id', None) or getattr(model, 'model', None)
            record = dict(stage='model', cycle=cycle, input=value, prompt=prompt,
                          source_observations=[source_record, *observations],
                          previous_iteration=str(previous.relative_to(directory)) if previous else None,
                          iteration=str(snapshot.relative_to(directory)),
                          model_identifier=model_identifier or (models or {}).get('model', {}).get('model_id')
                          or (backend_id if isinstance(backend_id, str) else type(model).__name__))
            try:
                if model is None:
                    raise ValueError('Model stage requires a configured local LLM backend')
                generate = getattr(model, 'generate', model)
                world_model = generate(prompt, **settings)
                if not isinstance(world_model, str) or not world_model.strip():
                    raise ValueError('Model returned an empty world model')
                # Publish a complete snapshot exclusively; never replace an earlier iteration.
                with NamedTemporaryFile(mode='w', encoding='utf-8', dir=snapshot.parent,
                                        prefix='.iteration-', delete=False) as output:
                    pending = Path(output.name)
                    try:
                        output.write(world_model)
                        output.flush()
                        os.fsync(output.fileno())
                        os.link(pending, snapshot)
                    finally:
                        pending.unlink(missing_ok=True)
                record.update(status='generated', output=world_model)
            except (Exception, KeyboardInterrupt) as error:
                record.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                              error=f'{type(error).__name__}: {error}', iteration=None)
                with (destination / 'model.json').open('x', encoding='utf-8') as output:
                    json.dump(record, output, ensure_ascii=False, indent=2)
                raise
            with (destination / 'model.json').open('x', encoding='utf-8') as output:
                json.dump(record, output, ensure_ascii=False, indent=2)
            goal_text = (directory / 'goal.md').read_text(encoding='utf-8')
            prompt, settings = stage_prompt('plan', world_model, goal=goal_text,
                                            available_actions=json.dumps(AVAILABLE_ACTIONS, indent=2))
            if 'max_output_tokens' in pipeline.get('planning', {}):
                settings['max_new_tokens'] = pipeline['planning']['max_output_tokens']
            if pipeline.get('planning', {}).get('reasoning_effort') is not None:
                settings['reasoning_effort'] = pipeline['planning']['reasoning_effort']
            record = dict(stage='plan', cycle=cycle, input=world_model, goal=goal_text,
                          model_identifier=plan_identifier or model_identifier or 'configured planner',
                          prompt=prompt, available_actions=AVAILABLE_ACTIONS, selected_action=None)
            try:
                if plan is None:
                    raise ValueError('Plan stage requires a schema-constrained LLM backend')
                generate = getattr(plan, 'generate', plan)
                text = generate(prompt, **settings, schema=ACTION_SCHEMA)
                if not isinstance(text, str):
                    raise ValueError('Planner must return a JSON string')
                record['output'] = text
                decision = json.loads(text)
                if (not isinstance(decision, dict) or set(decision) != {'action'}
                        or not isinstance(decision['action'], str) or decision['action'] not in AVAILABLE_ACTIONS):
                    raise ValueError('Planner must select exactly one supported action: look_left or look_right')
                record.update(status='generated', selected_action=decision['action'])
            except (Exception, KeyboardInterrupt) as error:
                record.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                              error=f'{type(error).__name__}: {error}')
                with (destination / 'plan.json').open('x', encoding='utf-8') as output:
                    json.dump(record, output, ensure_ascii=False, indent=2)
                raise
            with (destination / 'plan.json').open('x', encoding='utf-8') as output:
                json.dump(record, output, ensure_ascii=False, indent=2)
            action = decision['action']
            record = dict(stage='act', cycle=cycle, requested_action=action,
                          endpoint=AVAILABLE_ACTIONS[action]['endpoint'], goal={}, goal_accepted=None,
                          result_status=None, success=False)
            try:
                if act is None:
                    raise ValueError('Act stage requires a ROS action executor')
                act(action, record)
                if (record['goal_accepted'] is not True or record['result_status'] != 4
                        or record['success'] is not True):
                    raise RuntimeError('Action did not complete successfully')
                record['status'] = 'succeeded'
            except (Exception, KeyboardInterrupt) as error:
                record.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                              success=False, error=f'{type(error).__name__}: {error}')
                with (destination / 'act.json').open('x', encoding='utf-8') as output:
                    json.dump(record, output, ensure_ascii=False, indent=2)
                raise
            with (destination / 'act.json').open('x', encoding='utf-8') as output:
                json.dump(record, output, ensure_ascii=False, indent=2)
            metadata['completed_cycles'] += 1
            write_metadata(directory, metadata)
        status = 'completed'
        return directory
    except KeyboardInterrupt:
        status = 'interrupted'
        raise
    finally:
        try:
            if teardown:
                teardown()
        except BaseException:
            status = 'failed'
            raise
        finally:
            finalize_run(directory, metadata, status)


def run_cycle(observation: str, *, sense: Backend, model: Backend,
              plan: Backend, act: Backend) -> dict[str, str]:
    """Pass each stage's output to the next and return all intermediate results.

    Backends are supplied independently, so callers control selection and reuse.
    Exceptions propagate to the caller; a failed stage stops the cycle.
    """
    results = {}
    value = observation
    for name, backend in [('sense', sense), ('model', model), ('plan', plan), ('act', act)]:
        prompt, config = stage_prompt(name, value)
        value = backend.generate(prompt, **config)
        results[name] = value
    return results


def capture_once(node: Node, config: dict, output_dir: Path,
                 executor: SingleThreadedExecutor, timeout: float = 120) -> Path:
    """Receive one frame, request its description, and write the exact response."""
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.task import Future
    from sensor_msgs.msg import Image
    from smpa_ros_interfaces.srv import DescribeScene

    output_dir, _ = assigned_run(output_dir, config.get('run_id', ''))
    output_dir = output_dir / 'observations' / 'vision'
    prompt = files('smpa_ros.sense').joinpath(
        'prompts/' + config['prompt'] + '.txt',
    ).read_text(encoding='utf-8').strip()
    client = node.create_client(DescribeScene, '/sense')
    subscription = None
    try:
        node.get_logger().info('Waiting for /sense service')
        if not client.wait_for_service(timeout_sec=timeout):
            raise TimeoutError(f'/sense is unavailable after {timeout:g}s')
        image_future = Future()

        def receive(image: Image) -> None:
            if not image_future.done():
                image_future.set_result(image)

        node.get_logger().info(f"Subscribing to camera topic: {config['camera_topic']}")
        subscription = node.create_subscription(
            Image, config['camera_topic'], receive, qos_profile_sensor_data,
        )
        node.get_logger().info('Waiting for first image')
        started = perf_counter()
        executor.spin_until_future_complete(image_future, timeout_sec=timeout)
        if not image_future.done():
            raise TimeoutError(f"No image received on {config['camera_topic']} after {timeout:g}s")
        node.get_logger().info(f'Image received after {perf_counter() - started:.2f}s')
        node.destroy_subscription(subscription)
        subscription = None
        started = perf_counter()
        response_future = client.call_async(
            DescribeScene.Request(image=image_future.result(), prompt=prompt),
        )
        node.get_logger().info('Request sent to /sense')
        executor.spin_until_future_complete(response_future, timeout_sec=timeout)
        if not response_future.done():
            raise TimeoutError(f'/sense response timed out after {timeout:g}s')
        response = response_future.result()
        node.get_logger().info(f'/sense response received after {perf_counter() - started:.2f}s')
        if response.error:
            raise RuntimeError(response.error)
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
        path = output_dir / f'{timestamp}_sense_output.md'
        # Exclusive creation prevents an existing observation from being overwritten.
        with path.open('x', encoding='utf-8', newline='') as output:
            output.write(response.description)
        node.get_logger().info(f'Markdown file saved: {path.resolve()}')
        return path
    except Exception as error:
        node.get_logger().error(f'Camera-to-Sense pipeline failed: {error}')
        raise
    finally:
        if subscription is not None:
            node.destroy_subscription(subscription)
        node.destroy_client(client)


def execute_action(node, executor, action, record, timeout):
    """Send the default Look goal, record feedback/result, and cancel on early exit."""
    from action_msgs.msg import GoalStatus
    from rclpy.action import ActionClient
    from rosidl_runtime_py.convert import message_to_ordereddict
    if action not in AVAILABLE_ACTIONS:
        raise ValueError(f'Unsupported action: {action}')
    try:
        from simulation_actions.action import Look
    except ImportError as error:
        raise RuntimeError('simulation_actions/action/Look is unavailable; run ./install.sh to rebuild '
                           'the current Webots interfaces and source the workspace installation') from error
    client = ActionClient(node, Look, AVAILABLE_ACTIONS[action]['endpoint'])
    pending = handle = None
    result_future = None
    terminal = False

    def wait(future, phase):
        executor.spin_until_future_complete(future, timeout_sec=timeout)
        if not future.done():
            raise TimeoutError(f'{action}: timeout waiting for {phase}')
        return future.result()

    try:
        if not client.wait_for_server(timeout_sec=timeout):
            raise TimeoutError(f'{action}: action server unavailable')
        # Look's default angle is zero: the server selects its configured angle.
        goal = Look.Goal()
        record['goal'] = dict(message_to_ordereddict(goal))
        record['feedback'] = []

        def feedback(message):
            record['feedback'].append(dict(message_to_ordereddict(message.feedback)))

        node.get_logger().info(f"Sending {action}: {record['goal']}")
        pending = client.send_goal_async(goal, feedback_callback=feedback)
        handle = wait(pending, 'goal acceptance')
        record['goal_accepted'] = handle.accepted
        if not handle.accepted:
            raise RuntimeError(f'{action}: goal rejected')
        result_future = handle.get_result_async()
        response = wait(result_future, 'action result')
        terminal = True
        record.update(result_status=response.status,
                      result=dict(message_to_ordereddict(response.result)),
                      success=(response.status == GoalStatus.STATUS_SUCCEEDED and response.result.success))
        node.get_logger().info(f"{action}: ROS status {response.status}; result {record['result']}")
        if not record['success']:
            raise RuntimeError(f'{action}: unsuccessful ROS goal status/result {response.status}: {record["result"]}')
    finally:
        try:
            if pending is not None and not terminal:
                try:
                    if handle is None:
                        handle = wait(pending, 'late goal acceptance before cancellation')
                        record['goal_accepted'] = handle.accepted
                    if handle.accepted:
                        canceled = wait(handle.cancel_goal_async(), 'cancellation response')
                        record['cancel_requested'] = bool(canceled.goals_canceling)
                        if result_future is None:
                            result_future = handle.get_result_async()
                        response = wait(result_future, 'terminal result after cancellation')
                        record['cancellation_result_status'] = response.status
                except Exception as error:
                    record['cancellation_error'] = str(error)
                    node.get_logger().error(f'{action}: could not confirm goal termination: {error}')
        finally:
            client.destroy()


def main(args=None) -> None:
    from ament_index_python.packages import get_package_share_directory
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.signals import SignalHandlerOptions
    from rclpy.utilities import remove_ros_args

    parser = argparse.ArgumentParser(description='Execute one run of sequential SMPA cycles.')
    parser.add_argument('--config', type=Path, default=Path(
        get_package_share_directory('smpa_ros')) / 'config/pipeline.yaml')
    parser.add_argument('--world-model-root', type=Path, default=Path.cwd() / 'memory')
    parser.add_argument('--run-name', default='smpa')
    parser.add_argument('--max-cycles', type=int, default=None)
    parser.add_argument('--service', choices=['vision', 'speech', 'all'], default='vision')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--model-root', default=str(Path.cwd() / 'models'))
    parser.add_argument('--audio-lock', default='')
    parser.add_argument('--input-device', default='')
    parser.add_argument('--output-device', default='')
    parser.add_argument('--demo', choices=['true', 'false'], default='false')
    parser.add_argument('--timeout', type=float, default=120)
    options = parser.parse_args(remove_ros_args(args=args)[1:])
    config = yaml.safe_load(options.config.read_text(encoding='utf-8'))
    limit = options.max_cycles if options.max_cycles is not None else config.get('execution', {}).get('max_cycles', 1)
    if options.timeout <= 0:
        parser.error('--timeout must be positive')
    import math
    action_timeout = config.get('execution', {}).get('action_timeout', 30.0)
    if not isinstance(action_timeout, (int, float)) or isinstance(action_timeout, bool) or not math.isfinite(action_timeout) or action_timeout <= 0:
        parser.error('execution.action_timeout must be finite and positive')
    participant = None
    camera_node = executor = speech = None
    last_transcript = ''
    old_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)

    def setup(directory, identity):
        nonlocal participant, camera_node, executor, speech
        namespace = '/run_' + identity.replace('-', '_')
        command = ['ros2', 'launch', 'smpa_ros', 'llm.launch.py', 'participants_only:=true',
                   f'service:={options.service}', f'world_model_dir:={directory}', f'run_id:={identity}',
                   f'config:={options.config.resolve()}', f'python:={options.python}',
                   f'model_root:={options.model_root}']
        for name in ('audio_lock', 'input_device', 'output_device'):
            if getattr(options, name):
                command.append(f'{name}:={getattr(options, name)}')
        participant = subprocess.Popen(command, start_new_session=True)
        print(f'World-model run: {directory}\nROS endpoints: {namespace}/...', flush=True)
        rclpy.init(args=['--ros-args', '-r', f'/sense:={namespace}/sense',
                         '-r', f'/speech/listen:={namespace}/speech/listen',
                         '-r', f'/speech/talk:={namespace}/speech/talk',
                         '-p', f'world_model_dir:={directory}', '-p', f'run_id:={identity}'],
                   signal_handler_options=SignalHandlerOptions.NO)
        camera_node = rclpy.create_node('smpa_loop')
        executor = SingleThreadedExecutor()
        executor.add_node(camera_node)
        if options.service in ('speech', 'all'):
            from smpa_ros.speech.demo import ListenerTalker
            speech = ListenerTalker()

    def sense(directory, cycle):
        nonlocal last_transcript
        if participant.poll() is not None:
            raise RuntimeError('Participating ROS processes exited')
        outputs = {}
        if options.service in ('vision', 'all'):
            outputs['vision'] = capture_once(camera_node, config, directory, executor, options.timeout).read_text()
        if speech is not None:
            from smpa_ros_interfaces.action import Listen
            result = speech.perform(speech.listener, Listen.Goal(language='en', max_duration=10.0))
            last_transcript = result.transcription
            outputs['speech'] = result.transcription
        return json.dumps(outputs, ensure_ascii=False)

    def act(action, record):
        execute_action(camera_node, executor, action, record, action_timeout)
        if options.demo == 'true' and speech is not None:
            from smpa_ros_interfaces.action import Talk
            speech.perform(speech.talker, Talk.Goal(text=f'I heard {last_transcript}'))

    def teardown():
        try:
            if speech is not None:
                speech.close()
            if executor is not None:
                executor.shutdown()
            if camera_node is not None:
                camera_node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()
        finally:
            if participant is not None and participant.poll() is None:
                os.killpg(participant.pid, signal.SIGINT)
                try:
                    participant.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(participant.pid, signal.SIGTERM)
                    try:
                        participant.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(participant.pid, signal.SIGKILL)
                        participant.wait()

    registry = Path(get_package_share_directory('smpa_ros')) / 'models'
    registry_entries = yaml.safe_load((registry / 'model_library.yaml').read_text())['models']
    api_path = options.config.resolve().with_name('api.yaml')
    api_settings = None
    if (config['modeling'].get('backend') == 'api'
            or config.get('vision', {}).get('backend') == 'api'):
        api_settings = yaml.safe_load(api_path.read_text(encoding='utf-8'))
    if config['modeling'].get('backend') == 'api':
        selected_name = 'api'
        provider = config['modeling'].get('provider', 'openai')
        settings = api_settings if 'base_url' in api_settings else api_settings[provider]
        selected = {'backend': 'api', 'provider': provider, 'model_id': settings['models']['text']}
    else:
        selected_name = config['modeling']['model']
        selected = registry_entries[selected_name]
        if config['modeling'].get('backend', selected['backend']) != selected['backend']:
            raise ValueError('modeling.backend must match the selected registry backend or be api')
    modeling_backend = None

    def model(prompt, *, max_new_tokens, schema=None, reasoning_effort=None):
        nonlocal modeling_backend
        generation_options = {}
        if reasoning_effort is not None:
            if selected['backend'] != 'api':
                raise ValueError('reasoning_effort is supported only for API inference')
            generation_options['reasoning_effort'] = reasoning_effort
        if modeling_backend is None:
            if selected['backend'] == 'transformers':
                from smpa_ros.backends.transformers import TransformersBackend
                path = Path(options.model_root) / selected['path']
                modeling_backend = TransformersBackend(str(path), enable_thinking=selected.get('enable_thinking'))
            elif selected['backend'] == 'api':
                from smpa_ros.backends.api import APIBackend
                modeling_backend = APIBackend(api_path, modality='text', provider=selected['provider'])
            elif selected['backend'] == 'ollama':
                from smpa_ros.backends.ollama import OllamaBackend
                modeling_backend = OllamaBackend(selected['model_id'])
            else:
                raise ValueError(f"Unsupported modeling backend: {selected['backend']}")
        if schema is not None:
            if selected['backend'] not in ('transformers', 'api'):
                raise RuntimeError('Selected backend does not provide the required schema-constrained planning')
            return modeling_backend.generate(prompt, max_new_tokens=max_new_tokens, schema=schema, **generation_options)
        return modeling_backend.generate(prompt, max_new_tokens=max_new_tokens, **generation_options)

    models = {'model': dict(selected, name=selected_name)}
    if options.service in ('vision', 'all'):
        if config.get('vision', {}).get('backend') == 'api':
            provider = config['vision'].get('provider', 'openai')
            settings = api_settings if 'base_url' in api_settings else api_settings[provider]
            models['vision'] = {'backend': 'api', 'provider': provider, 'model_id': settings['models']['vision']}
        else:
            models['vision'] = yaml.safe_load((registry / (config['model'] + '.yaml')).read_text())
    if options.service in ('speech', 'all'):
        models.update(yaml.safe_load((registry / 'model_library.yaml').read_text())['models'])
    try:
        run(limit, root=options.world_model_root, run_name=options.run_name, sense=sense, model=model,
            model_identifier=selected['model_id'],
            plan=model, act=act, plan_identifier=selected['model_id'], goal=config.get('goal', DEFAULT_GOAL),
            models=models, configuration=dict(pipeline=config, service=options.service, max_cycles=limit),
            setup=setup, teardown=teardown)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)


if __name__ == '__main__':
    main()
