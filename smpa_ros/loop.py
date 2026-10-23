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


def initialize_run(root, run_name='session', *, models=None, configuration=None):
    name = re.sub(r'[^a-zA-Z0-9_-]+', '_', run_name).strip('_-')[:80]
    if not name:
        raise ValueError('run_name must contain a letter, digit, underscore, or hyphen')
    identity = str(uuid4())
    started = datetime.now().astimezone()
    directory = Path(root).resolve() / 'runs' / identity
    directory.mkdir(parents=True, exist_ok=False)
    for category in ('observations/vision', 'observations/audio', 'world_model', 'state'):
        (directory / category).mkdir(parents=True)
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


def stage_prompt(name, value, previous_world_model=''):
    component = files('smpa_ros').joinpath(name)
    prompt = component.joinpath('prompts/default.txt').read_text(encoding='utf-8').format(input=value, previous_world_model=previous_world_model)
    settings = json.loads(component.joinpath('models/default.json').read_text(encoding='utf-8'))
    return prompt, settings


def run(max_cycles=1, *, root=None, run_name='session', sense,
        model=None, plan=None, act=None, models=None, configuration=None,
        setup=None, teardown=None, model_identifier=None):
    """Own the entire session. Each sense callback receives its run and cycle number.

    Model creates a full immutable snapshot; Plan and Act retain their placeholders.
    Results are committed after each stage; the count advances only after Act succeeds.
    """
    if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1:
        raise ValueError('max_cycles must be a positive integer')
    directory, metadata = initialize_run(root or Path.cwd() / 'memory', run_name,
                                         models=models, configuration=configuration)
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
            value = world_model
            for name, backend in [('plan', plan), ('act', act)]:
                prompt, settings = stage_prompt(name, value)
                generate = getattr(backend, 'generate', backend)
                result = generate(prompt, **settings) if backend is not None else value
                record = dict(stage=name, cycle=cycle, status='generated' if backend is not None else 'placeholder',
                              input=value, prompt=prompt, output=result)
                with (destination / f'{name}.json').open('x', encoding='utf-8') as output:
                    json.dump(record, output, ensure_ascii=False, indent=2)
                value = result
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
    parser.add_argument('--run-name', default='session')
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
        if options.service in ('vision', 'all'):
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
        if camera_node is not None:
            outputs['vision'] = capture_once(camera_node, config, directory, executor, options.timeout).read_text()
        if speech is not None:
            from smpa_ros_interfaces.action import Listen
            result = speech.perform(speech.listener, Listen.Goal(language='en', max_duration=10.0))
            last_transcript = result.transcription
            outputs['speech'] = result.transcription
        return json.dumps(outputs, ensure_ascii=False)

    def speak(prompt, *, max_new_tokens):
        from smpa_ros_interfaces.action import Talk
        speech.perform(speech.talker, Talk.Goal(text=f'I heard {last_transcript}'))
        return f'I heard {last_transcript}'

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
    selected_name = config['modeling']['model']
    selected = registry_entries[selected_name]
    modeling_backend = None

    def model(prompt, *, max_new_tokens):
        nonlocal modeling_backend
        if modeling_backend is None:
            if selected['backend'] == 'transformers':
                from smpa_ros.backends.transformers import TransformersBackend
                path = Path(options.model_root) / selected['path']
                modeling_backend = TransformersBackend(str(path), enable_thinking=selected.get('enable_thinking'))
            elif selected['backend'] == 'ollama':
                from smpa_ros.backends.ollama import OllamaBackend
                modeling_backend = OllamaBackend(selected['model_id'])
            else:
                raise ValueError(f"Unsupported modeling backend: {selected['backend']}")
        return modeling_backend.generate(prompt, max_new_tokens=max_new_tokens)

    models = {'model': dict(selected, name=selected_name)}
    if options.service in ('vision', 'all'):
        models['vision'] = yaml.safe_load((registry / (config['model'] + '.yaml')).read_text())
    if options.service in ('speech', 'all'):
        models.update(yaml.safe_load((registry / 'model_library.yaml').read_text())['models'])
    try:
        run(limit, root=options.world_model_root, run_name=options.run_name, sense=sense, model=model,
            model_identifier=selected['model_id'],
            act=speak if options.demo == 'true' and options.service in ('speech', 'all') else None,
            models=models, configuration=dict(pipeline=config, service=options.service, max_cycles=limit),
            setup=setup, teardown=teardown)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)


if __name__ == '__main__':
    main()
