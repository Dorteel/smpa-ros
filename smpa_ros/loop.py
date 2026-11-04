"""Own one run and execute sequential Sense–Model–Plan–Act cycles."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from importlib.resources import files
from pathlib import Path

import os
import re
import signal
import subprocess
import sys
from tempfile import NamedTemporaryFile
from uuid import uuid4
import yaml

from smpa_ros.backends.base import Backend
from smpa_ros.act import execute_action, perform_action, load_prompt as act_prompt
from smpa_ros.model import generate_world_model, load_prompt as model_prompt
from smpa_ros.plan import (load_prompt as plan_prompt, select_action,
                           validate_settings as validate_plan_settings)

DEFAULT_GOAL = '# Goal\n\nExplore the environment to discover previously unobserved objects and relationships.\n'


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
    component = files('smpa_ros')
    prompt_path = 'prompts/sense/default.txt' if name == 'sense' else name + '/prompts/default.txt'
    settings_path = 'models/sense/default.json' if name == 'sense' else name + '/models/default.json'
    prompt = component.joinpath(prompt_path).read_text(encoding='utf-8').format(input=value, previous_world_model=previous_world_model, goal=goal, available_actions=available_actions)
    settings = json.loads(component.joinpath(settings_path).read_text(encoding='utf-8'))
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
    stage = 'modeling'
    effort = pipeline.get(stage, {}).get('reasoning_effort')
    if effort is not None and effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh'):
        raise ValueError(f'{stage}.reasoning_effort is unsupported')
    if 'max_output_tokens' in pipeline.get(stage, {}):
        limit = pipeline[stage]['max_output_tokens']
        if type(limit) is not int or limit <= 0:
            raise ValueError(f'{stage}.max_output_tokens must be a positive integer')
    validate_plan_settings(pipeline.get('planning', {}))
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
            world_model = generate_world_model(
                directory, cycle, value, model, observations=observations, pipeline=pipeline,
                models=models, model_identifier=model_identifier)
            action = select_action(directory, cycle, world_model, plan, pipeline=pipeline,
                                   plan_identifier=plan_identifier, model_identifier=model_identifier)
            perform_action(directory, cycle, action, act)
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
        if name == 'model':
            prompt, config = model_prompt(value)
        elif name == 'plan':
            prompt, config = plan_prompt(value)
        elif name == 'act':
            prompt, config = act_prompt(value)
        else:
            prompt, config = stage_prompt(name, value)
        value = backend.generate(prompt, **config)
        results[name] = value
    return results


def main(args=None) -> None:
    from ament_index_python.packages import get_package_share_directory
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.signals import SignalHandlerOptions
    from rclpy.utilities import remove_ros_args

    parser = argparse.ArgumentParser(description='Execute one run of sequential SMPA cycles.')
    parser.add_argument('--config', type=Path, default=Path(
        get_package_share_directory('smpa_ros')) / 'config.yaml')
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
            from smpa_ros.sense import capture_once
            outputs['vision'] = capture_once(camera_node, config, directory, executor, options.timeout).read_text()
        if speech is not None:
            from smpa_ros_interfaces.action import Listen
            result = speech.perform(speech.listener, Listen.Goal(language='en', max_duration=10.0))
            last_transcript = result.transcription
            outputs['speech'] = result.transcription
        return json.dumps(outputs, ensure_ascii=False)

    def act(action, record):
        execute_action(camera_node, executor, action, record, action_timeout,
                       speech=speech if options.demo == 'true' else None, transcript=last_transcript)

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
    if config['modeling'].get('backend') == 'api':
        selected_name = 'api'
        provider = config['modeling'].get('provider', 'openai')
        settings = config[provider]
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
                modeling_backend = APIBackend(options.config, modality='text', provider=selected['provider'])
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
            settings = config[provider]
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
