"""Run directories, assignment validation, and lifecycle metadata."""
from datetime import datetime
from contextlib import contextmanager
from copy import deepcopy
from urllib.parse import urlsplit
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from uuid import uuid4

import yaml


class MemoryManager:
    """Own one run's identity and metadata without owning stage artifacts."""

    def __init__(self, root, run_name='smpa', *, models=None, configuration=None, goal):
        configuration = deepcopy(configuration or {})
        credentials = dict(os.environ)
        env_path = Path.cwd() / '.env'
        if env_path.is_file():
            from dotenv import dotenv_values
            credentials = {**dotenv_values(env_path), **credentials}
        pipeline = configuration.get('pipeline', configuration)
        key_names = {entry['api_key_env'] for entry in pipeline.values()
                     if isinstance(entry, dict) and isinstance(entry.get('api_key_env'), str)}
        # Reject inline credentials rather than silently persisting or changing them.
        def check(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized = str(key).lower()
                    if (any(word in normalized for word in ('api_key', 'password', 'secret', 'authorization', 'access_token', 'credential'))
                            or normalized in ('token', 'key', 'api_token', 'bearer_token')) and not normalized.endswith('_env'):
                        raise ValueError('Credentials must be supplied through environment variables')
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
            elif isinstance(value, str):
                if value.startswith(('http://', 'https://')):
                    url = urlsplit(value)
                    if url.username or url.password or url.query or url.fragment:
                        raise ValueError('Credential-bearing URLs are forbidden in run configuration')
                for key, secret in credentials.items():
                    if (key in key_names or any(word in key.upper() for word in ('API_KEY', 'PASSWORD', 'SECRET', 'TOKEN', 'CREDENTIAL'))) and secret and secret in value:
                        raise ValueError('Credential data is forbidden in run configuration')
        check(configuration)
        check(models or {})
        check(goal)
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
        parameters = deepcopy(configuration.get('pipeline', configuration))
        if 'pipeline' in configuration and 'stage_prompts' in configuration:
            parameters['stage_prompts'] = deepcopy(configuration['stage_prompts'])
        if 'stage_settings' in configuration and 'pipeline' in configuration:
            from smpa_ros.backends.base import GENERATION_KEYS
            parameters['stage_settings'] = deepcopy(configuration['stage_settings'])
            for section in ('vision', 'modeling', 'planning'):
                for key in (*GENERATION_KEYS, 'output_schema'):
                    parameters.get(section, {}).pop(key, None)
        from smpa_ros.plan import enabled_actions, refinement_settings
        parameters['enabled_actions'] = list(enabled_actions(parameters))
        if 'scene_refinement' in parameters:
            parameters['scene_refinement'] = refinement_settings(parameters)
        parameters['_run'] = dict(goal=goal, models=deepcopy(models or {}),
                                  enabled_actions=list(enabled_actions(parameters)))
        if 'pipeline' in configuration:
            parameters['_run'].update({key: value for key, value in configuration.items()
                                       if key not in ('pipeline', 'stage_prompts', 'stage_settings')})
        with (directory / 'parameters.yaml').open('x', encoding='utf-8') as output:
            yaml.safe_dump(parameters, output, sort_keys=False)
        metadata = dict(run_id=identity, run_name=name, started_at=started.isoformat(),
                        completed_at=None, status='running', completed_cycles=0,
                        models=models or {}, configuration=configuration or {})
        self.cycle = 0
        self.in_cycle = False
        self.counts = {}
        self.sequence = 0
        self.previous_record = None
        self.directory = directory
        self.metadata = metadata
        self.write_metadata()

    @classmethod
    def resume_conversation(cls, directory):
        """Restore a conversational run without creating or replacing its artifacts.

        Robot runs are deliberately excluded: resuming physical experiments would
        require a separate decision about their execution semantics.
        """
        import json
        path, _ = cls.assigned_run(str(Path(directory).resolve()))
        metadata = yaml.safe_load((path / 'metadata.yaml').read_text())
        if metadata.get('configuration', {}).get('kind') != 'conversation':
            raise ValueError('Only conversational runs can be resumed here')
        instance = cls.__new__(cls)
        instance.directory, instance.metadata = path, metadata
        cycles = sorted((path / 'state').glob('cycle_*'))
        instance.cycle = max((int(item.name.split('_')[1]) for item in cycles), default=0)
        instance.in_cycle = False
        instance.counts = {}
        records = [(json.loads(record.read_text()), str(record.relative_to(path)))
                   for cycle in cycles for record in cycle.glob('*.json')]
        latest = max(records, key=lambda item: item[0].get('sequence', 0), default=({}, None))
        instance.sequence = latest[0].get('sequence', 0)
        instance.previous_record = latest[1]
        instance.metadata.update(status='running', completed_at=None)
        instance.write_metadata()
        return instance

    @staticmethod
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

    def write_metadata(self):
        # Runner supplies measured execution details; persistence stays here.
        runtime = self.metadata.get('configuration', {}).get('runtime')
        if runtime is not None:
            parameters = self.directory / 'parameters.yaml'
            frozen = yaml.safe_load(parameters.read_text())
            if frozen.get('_run', {}).get('runtime') != runtime:
                frozen['_run']['runtime'] = deepcopy(runtime)
                with NamedTemporaryFile(mode='w', dir=self.directory, prefix='.parameters-', delete=False) as output:
                    pending = Path(output.name)
                    try:
                        yaml.safe_dump(frozen, output, sort_keys=False)
                        output.flush()
                        os.fsync(output.fileno())
                        pending.replace(parameters)
                    finally:
                        pending.unlink(missing_ok=True)
        with NamedTemporaryFile(mode='w', dir=self.directory, prefix='.metadata-', delete=False) as output:
            pending = Path(output.name)
            try:
                yaml.safe_dump(self.metadata, output, sort_keys=False)
                output.flush()
                os.fsync(output.fileno())
                pending.replace(self.directory / 'metadata.yaml')
            finally:
                pending.unlink(missing_ok=True)

    def write_refinement(self, state):
        """Persist the current finite question queue and its answer provenance."""
        import json
        with NamedTemporaryFile(mode='w', dir=self.directory / 'state', prefix='.refinement-', delete=False) as output:
            pending = Path(output.name)
            try:
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
                pending.replace(self.directory / 'state/refinement.json')
            finally:
                pending.unlink(missing_ok=True)

    def begin_cycle(self):
        if self.metadata['status'] != 'running' or self.in_cycle:
            raise RuntimeError('A cycle requires an active run and cannot be nested')
        self.cycle += 1
        self.counts = {}
        self.in_cycle = True
        (self.directory / 'state' / f'cycle_{self.cycle:06d}').mkdir()

    def record_path(self, name):
        return Path('state') / f'cycle_{self.cycle:06d}' / name

    def invocation(self, stage, prompt, source=None):
        if not self.in_cycle or self.metadata['status'] != 'running':
            raise RuntimeError('Stage calls require an active cycle')
        count = self.counts.get(stage, 0) + 1
        self.counts[stage] = count
        self.sequence += 1
        name = f'{stage}.json' if count == 1 else f'{stage}_{count:03d}.json'
        provenance = dict(sequence=self.sequence, invocation=count,
                          started_at=datetime.now().astimezone().isoformat(),
                          prompt_name=prompt, source_record=source,
                          previous_record=self.previous_record)
        self.previous_record = str(self.record_path(name))
        return name, provenance

    def complete_cycle(self):
        self.metadata['completed_cycles'] += 1
        self.write_metadata()

    def finalize(self, status):
        if status not in ('completed', 'interrupted', 'failed'):
            raise ValueError(f'Invalid terminal run status: {status}')
        if self.metadata['status'] == 'running':
            self.metadata.update(status=status, completed_at=datetime.now().astimezone().isoformat())
            self.write_metadata()


@contextmanager
def locked_run(root, run_id):
    """Resolve one run identity and hold its cooperative filesystem write lock.

    The root is the configured memory root and run_id identifies one existing
    run beneath its runs directory. Require a canonical UUID, locate exactly one
    matching directory, and validate its metadata and expected subdirectories.
    Resolve the directory before opening the lock so a caller cannot select a
    different run by supplying a path as the identity.

    Acquire an exclusive advisory lock in the run directory and validate the
    identity again after acquiring it. Yield the validated run directory while
    the caller reads or updates its records. Release the lock and close its file
    descriptor even if the caller raises. All writers that share mutable files
    must use this same lock; atomic replacement alone does not serialize them.
    The lock does not create a run, resume an experiment, or change metadata.
    """
    from uuid import UUID
    import fcntl

    try:
        identity = str(UUID(run_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('run_id must be a canonical UUID') from error
    if identity != run_id:
        raise ValueError('run_id must be a canonical UUID')
    runs = Path(root).expanduser().resolve() / 'runs'
    matches = [path for path in runs.glob('*_' + identity)
               if path.is_dir() and not path.is_symlink()]
    if len(matches) != 1:
        raise ValueError('run_id must identify exactly one existing run')
    directory, _ = MemoryManager.assigned_run(str(matches[0].resolve()), identity)
    descriptor = os.open(directory / '.run.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        MemoryManager.assigned_run(str(directory), identity)
        yield directory
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
