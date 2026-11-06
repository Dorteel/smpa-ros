"""Configure and execute SMPA with ROS participants and run persistence."""
from __future__ import annotations

import argparse
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
import os
import signal
import subprocess
import sys

import yaml

from contextlib import contextmanager
from threading import current_thread, main_thread

from memory import MemoryManager
from smpa_ros.backends.base import GENERATION_KEYS, stage_generation
from smpa_ros.act import execute_action, perform_action
from smpa_ros.model import (generate_world_model, resolve_vision_model,
                            unify_model, validate_settings as validate_model_settings)
from smpa_ros.plan import (ACTION_SCHEMA, AVAILABLE_ACTIONS, select_action,
                           validate_settings as validate_plan_settings)


class Runner:
    """Wire cognitive stages to their backends, ROS participants, and memory."""

    def __init__(self, args=None, *, max_cycles=1, root=None, run_name='smpa',
                 sense=None, model=None, plan=None, act=None, models=None,
                 configuration=None, setup=None, teardown=None,
                 model_identifier=None, plan_identifier=None):
        self.participant = None
        self.camera_node = self.executor = self.speech = self.sense_node = None
        self.backend = None
        self.last_transcript = ''

        self.memory = None
        self._signal = None
        self._sense, self._model, self._plan, self._act = sense, model, plan, act
        self._setup, self._teardown = setup, teardown
        self.root, self.run_name = root or Path.cwd() / 'memory', run_name
        self.models, self.configuration = models, configuration or {}
        self.model_identifier, self.plan_identifier = model_identifier, plan_identifier
        self.limit = max_cycles
        if sense is None:
            self.configure(args)
            self.initialize_backends()
            self.root, self.run_name = self.options.world_model_root, self.options.run_name
            self.configuration = dict(pipeline=self.config, service=self.options.service, max_cycles=self.limit,
                                      available_actions=deepcopy(AVAILABLE_ACTIONS),
                                      output_schema=deepcopy(self.config['stage_settings']['plan'].get('output_schema', ACTION_SCHEMA)))
            self.configuration['stage_settings'] = deepcopy(self.config['stage_settings'])
            self.configuration['stage_models'] = {
                'sense': deepcopy(self.models.get('vision', {})),
                'model': deepcopy(self.models.get('model', {})),
                'plan': deepcopy(self.models.get('plan', self.models.get('model', {}))),
                'act': dict(backend='ros_action', model_id=None, provider=None)}
            for stage, model in self.configuration['stage_models'].items():
                model.setdefault('provider', None)
                if stage == 'sense':
                    model.setdefault('backend', self.config.get('vision', {}).get('backend', 'transformers'))
            if 'stage_prompts' not in self.original_config:
                self.configuration['stage_prompts'] = self.config['stage_prompts']
            self._sense, self._act = self.observe, self.dispatch
            self._setup, self._teardown = self.setup, self.teardown
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError('max_cycles must be a positive integer')
        self.pipeline = self.config if hasattr(self, 'config') else self.configuration.get('pipeline', {})
        validate_model_settings(self.pipeline.get('modeling', {}))
        validate_plan_settings(self.pipeline.get('planning', {}))

    def __enter__(self):
        if current_thread() is main_thread():
            self._signal = signal.getsignal(signal.SIGTERM)
            def interrupt(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, interrupt)
        return self

    def __exit__(self, kind, error, traceback):
        try:
            self.finish('interrupted' if isinstance(error, KeyboardInterrupt) else
                        'failed' if error is not None else 'completed')
        finally:
            if self._signal is not None:
                signal.signal(signal.SIGTERM, self._signal)
                self._signal = None

    def configure(self, args=None):
        from ament_index_python.packages import get_package_share_directory
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
        self.options = options
        self.original_config = deepcopy(config)
        env_path = options.config.resolve().parent / '.env'
        if env_path.is_file():
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        config = deepcopy(config)
        prompts = config.setdefault('stage_prompts', {})
        for stage in ('sense', 'model', 'plan', 'act'):
            name = config.get('prompt', 'describe_scene') if stage == 'sense' else 'default'
            if stage not in prompts:
                prompts[stage] = dict(name=name, text=files('smpa_ros').joinpath(
                    f'prompts/{stage}/{name}.txt').read_text(encoding='utf-8'))
        effective = {stage: stage_generation(config, stage) for stage in ('sense', 'model', 'plan', 'act')}
        config['stage_settings'] = effective
        for section in ('vision', 'modeling', 'planning'):
            for key in (*GENERATION_KEYS, 'output_schema'):
                config.get(section, {}).pop(key, None)
        self.config = unify_model(config)
        self.limit = limit
        self.action_timeout = action_timeout

    def setup(self, directory, identity):
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.signals import SignalHandlerOptions
        namespace = '/run_' + identity.replace('-', '_')
        command = ['ros2', 'launch', 'smpa_ros', 'llm.launch.py', 'participants_only:=true',
                   'service:=speech', f'world_model_dir:={directory}', f'run_id:={identity}',
                   f'config:={self.options.config.resolve()}', f'python:={self.options.python}',
                   f'model_root:={self.options.model_root}']
        for name in ('audio_lock', 'input_device', 'output_device'):
            if getattr(self.options, name):
                command.append(f'{name}:={getattr(self.options, name)}')
        if self.options.service in ('speech', 'all'):
            self.participant = subprocess.Popen(command, start_new_session=True)
        print(f'World-model run: {directory}\nROS endpoints: {namespace}/...', flush=True)
        rclpy.init(args=['--ros-args', '-r', f'/sense:={namespace}/sense',
                         '-r', f'/speech/listen:={namespace}/speech/listen',
                         '-r', f'/speech/talk:={namespace}/speech/talk',
                         '-p', f'config:={self.options.config.resolve()}',
                         '-p', f'world_model_dir:={directory}', '-p', f'run_id:={identity}'],
                   signal_handler_options=SignalHandlerOptions.NO)
        self.camera_node = rclpy.create_node('smpa_loop')
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.camera_node)
        if self.selected['backend'] == 'transformers':
            from smpa_ros.backends.transformers import TransformersVLM
            self.backend = TransformersVLM(self.selected['path'], self.selected.get('dtype', 'auto'),
                                           device=self.selected.get('device', 'auto'))
        else:
            from smpa_ros.backends.api import APIBackend
            self.backend = APIBackend(self.options.config, modality='vision', provider=self.selected['provider'])
            self.backend.model_id = self.selected['model_id']
        self._model = self._plan = self.backend
        if self.options.service in ('vision', 'all'):
            from smpa_ros.sense import SenseService
            self.sense_node = SenseService(backend=self.backend)
            self.executor.add_node(self.sense_node)
        if self.selected['backend'] == 'transformers':
            runtime = dict(device=str(self.backend.model.device), dtype=str(self.backend.model.dtype))
            self.memory.metadata['configuration']['runtime'] = runtime
            self.memory.write_metadata()
        if self.options.service in ('speech', 'all'):
            from speech.demo import ListenerTalker
            self.speech = ListenerTalker()

    def observe(self, directory, cycle, prompt=None):
        if self.participant is not None and self.participant.poll() is not None:
            raise RuntimeError('Participating ROS processes exited')
        from smpa_ros.sense import observe
        observation, self.last_transcript = observe(
            self.camera_node, dict(self.config, prompt=prompt or self.config['prompt']), directory, self.executor, self.options.timeout, self.options.service, self.speech)
        return observation

    def dispatch(self, action, record):
        execute_action(self.camera_node, self.executor, action, record, self.action_timeout,
                       speech=self.speech if self.options.demo == 'true' else None, transcript=self.last_transcript)

    def teardown(self):
        import rclpy
        try:
            if self.speech is not None:
                self.speech.close()
            if self.executor is not None:
                self.executor.shutdown()
            if self.sense_node is not None:
                self.sense_node.destroy_node()
                self.sense_node = None
            if self.camera_node is not None:
                self.camera_node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()
        finally:
            if self.backend is not None:
                self._model = self._plan = self.backend = None
            if self.participant is not None and self.participant.poll() is None:
                for signum, timeout in ((signal.SIGINT, 15), (signal.SIGTERM, 5),
                                        (signal.SIGKILL, None)):
                    os.killpg(self.participant.pid, signum)
                    try:
                        self.participant.wait(timeout=timeout)
                        break
                    except subprocess.TimeoutExpired:
                        continue

    def initialize_backends(self):
        from ament_index_python.packages import get_package_share_directory

        registry = Path(get_package_share_directory('smpa_ros')) / 'models'
        selected = dict(self.config['model'])
        if selected['backend'] == 'transformers':
            selected = resolve_vision_model(self.config, registry, self.options.model_root)
        elif selected['backend'] != 'api':
            raise ValueError('The global model must support both images and text (Transformers VLM or API)')
        self.config['model'] = selected
        self.selected = selected
        self.models = {stage: deepcopy(selected) for stage in ('vision', 'model', 'plan')}
        self.model_identifier = self.plan_identifier = selected['model_id']
        if self.options.service in ('speech', 'all'):
            # Speech recognition/synthesis are separate from cognitive inference.
            entries = yaml.safe_load((registry / 'model_library.yaml').read_text())['models']
            self.models.update({name: entry for name, entry in entries.items()
                                if name not in self.models})

    def start(self, *, goal):
        """Create one independent run; use inside the Runner context for cleanup."""
        if self.memory is not None and self.memory.metadata['status'] == 'running':
            raise RuntimeError('Finish the active run before starting another')
        self.memory = MemoryManager(self.root, self.run_name, models=self.models,
                                    configuration=self.configuration, goal=goal)
        self.memory.metadata['max_cycles'] = self.limit
        self.memory.write_metadata()
        self.sources = {}
        self.origins = {}
        self.previous_snapshot = ''
        self.last_transcript = ''
        if hasattr(self, 'options'):
            # All participants and lazy API backends read this run's frozen copy.
            self.options.config = self.memory.directory / 'parameters.yaml'
            self.initialize_backends()
        if self._setup:
            self._setup(self.memory.directory, self.memory.metadata['run_id'])
        return self.memory.directory

    @contextmanager
    def cycle(self):
        """Mark one explicit script-defined cycle complete only on success."""
        self.memory.begin_cycle()
        try:
            yield
            self.memory.complete_cycle()
        finally:
            self.memory.in_cycle = False

    def sense(self, *, prompt=None):
        from smpa_ros.sense import record_observation
        prompt = self.pipeline.get('prompt', 'describe_scene') if prompt is None else prompt
        name, provenance = self.memory.invocation('sense', prompt)
        callback = self._sense
        if getattr(callback, '__self__', None) is self:
            callback = lambda directory, cycle: self.observe(directory, cycle, prompt)
        value, sources = record_observation(
            self.memory.directory, self.memory.cycle, callback,
            record_name=name, provenance=provenance, prompt_name=prompt, configuration=self.pipeline)
        self.origins[value] = str(self.memory.record_path(name))
        self.sources[value] = sources
        return value

    def model(self, observation, *, prompt='default'):
        if prompt == 'default':
            prompt = self.pipeline.get('stage_prompts', {}).get('model', {}).get('name', prompt)
        name, provenance = self.memory.invocation('model', prompt, self.origins.get(observation))
        value = generate_world_model(
            self.memory.directory, self.memory.cycle, observation, self._model,
            observations=self.sources.get(observation, []), pipeline=self.pipeline, models=self.models,
            model_identifier=self.model_identifier, prompt_name=prompt, record_name=name,
            previous_snapshot=self.previous_snapshot, provenance=provenance)
        suffix = name[len('model'):-len('.json')]
        self.previous_snapshot = f'world_model/iteration_{self.memory.cycle:03d}{suffix}.md'
        self.origins[value] = str(self.memory.record_path(name))
        return value

    def plan(self, world, *, prompt='default'):
        if prompt == 'default':
            prompt = self.pipeline.get('stage_prompts', {}).get('plan', {}).get('name', prompt)
        name, provenance = self.memory.invocation('plan', prompt, self.origins.get(world))
        action = select_action(
            self.memory.directory, self.memory.cycle, world, self._plan,
            pipeline=self.pipeline, plan_identifier=self.plan_identifier,
            model_identifier=self.model_identifier, prompt_name=prompt,
            record_name=name, provenance=provenance)
        self.origins[action] = str(self.memory.record_path(name))
        return action

    def act(self, action, *, prompt='default'):
        if prompt == 'default':
            prompt = self.pipeline.get('stage_prompts', {}).get('act', {}).get('name', prompt)
        name, provenance = self.memory.invocation('act', prompt, self.origins.get(action))
        perform_action(self.memory.directory, self.memory.cycle, action, self._act,
                       prompt_name=prompt, record_name=name, provenance=provenance, configuration=self.pipeline)

    def finish(self, status='completed'):
        """Stop participants and finalize the active run, including cleanup failures."""
        if self.memory is None or self.memory.metadata['status'] != 'running':
            return
        try:
            if self._teardown:
                self._teardown()
        except BaseException:
            status = 'failed'
            raise
        finally:
            self.memory.finalize(status)
            self.participant = None
            self.camera_node = self.executor = self.speech = self.sense_node = None
        self.backend = None
