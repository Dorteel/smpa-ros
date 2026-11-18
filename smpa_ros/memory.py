"""Run directories, assignment validation, and lifecycle metadata."""
from datetime import datetime
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from urllib.parse import urlsplit
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import local
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
        instance._metadata_snapshot = deepcopy(metadata)
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
        """Publish this manager's metadata changes without losing another writer's.

        A new run is private until its first metadata file is published. For an
        existing run, hold its cooperative lock while reading the latest file and
        comparing it with this manager's last successful snapshot. Combine edits
        to different dictionary keys, including nested keys. If two writers change
        the same value differently, reject the stale write and retain the existing
        file; callers can reload and decide how to proceed.

        Preserve atomic replacement and the existing runtime entry in the frozen
        parameters. Once the metadata file is durable, refresh this manager's
        snapshot and its visible dictionary to include other completed edits.
        """
        missing = object()

        def combine(previous, desired, current):
            """Merge independent changes and reject incompatible concurrent edits.

            Recurse through dictionaries to preserve changes to separate fields.
            Treat lists and other values as indivisible. A deleted field remains
            deleted only if another writer did not change it since the snapshot.
            """
            if previous == desired:
                return current
            if current == previous or desired == current:
                return desired
            if all(isinstance(value, dict) for value in (previous, desired, current)):
                merged = dict(current)
                for key in set(previous) | set(desired):
                    value = combine(previous.get(key, missing), desired.get(key, missing),
                                    current.get(key, missing))
                    if value is missing:
                        merged.pop(key, None)
                    else:
                        merged[key] = value
                return merged
            raise RuntimeError('Concurrent run metadata update conflicts with this write')

        destination = self.directory / 'metadata.yaml'
        lock = (locked_assigned_run(self.directory, self.metadata['run_id'])
                if destination.exists() else nullcontext())
        with lock:
            if destination.exists():
                current = yaml.safe_load(destination.read_text())
                if not isinstance(current, dict) or current.get('run_id') != self.metadata['run_id']:
                    raise ValueError('Run metadata identity changed during update')
                previous = getattr(self, '_metadata_snapshot', current)
                merged = deepcopy(combine(previous, self.metadata, current))
            else:
                merged = deepcopy(self.metadata)
            runtime = merged.get('configuration', {}).get('runtime')
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
                    yaml.safe_dump(merged, output, sort_keys=False)
                    output.flush()
                    os.fsync(output.fileno())
                    pending.replace(destination)
                finally:
                    pending.unlink(missing_ok=True)
            self.metadata.clear()
            self.metadata.update(deepcopy(merged))
            self._metadata_snapshot = deepcopy(merged)

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
        """Reserve one durable stage record and its run-wide provenance.

        The experiment must have an active cycle. Delegate allocation to shared
        run memory so independent processes cannot choose the same name or
        sequence. Mirror the returned values in this manager for existing callers
        that inspect the current cycle, counters, or previous record.
        """
        if not self.in_cycle or self.metadata['status'] != 'running':
            raise RuntimeError('Stage calls require an active cycle')
        cycle, name, provenance = reserve_invocation(
            self.directory, self.metadata['run_id'], stage, prompt, source,
            expected_cycle=self.cycle)
        self.counts[stage] = provenance['invocation']
        self.sequence = provenance['sequence']
        self.previous_record = str(Path('state') / f'cycle_{cycle:06d}' / name)
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


_held_run_locks = local()


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
    Reentering the same lock on the same thread reuses its outer acquisition;
    other threads and processes must wait. The lock does not create a run,
    resume an experiment, or change metadata.
    """
    from uuid import UUID

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
    with locked_assigned_run(directory, identity):
        yield directory


@contextmanager
def locked_assigned_run(directory, run_id):
    """Lock an already assigned run without changing where it is stored.

    Validate the absolute directory and its run identity using the existing
    assignment contract. Acquire its advisory lock, then validate it again so
    waiting writers cannot proceed using a mismatched run. The same thread may
    enter the lock again; other threads and processes wait for the outer context.
    Release the descriptor on exit, including when validation or a caller fails.
    """
    import fcntl

    directory, identity = MemoryManager.assigned_run(str(directory), run_id)
    directory = directory.resolve()
    held = getattr(_held_run_locks, 'directories', None)
    if held is None:
        held = _held_run_locks.directories = set()
    if directory in held:
        yield directory
        return
    descriptor = os.open(directory / '.run.lock', os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        acquired = True
        held.add(directory)
        MemoryManager.assigned_run(str(directory), identity)
        yield directory
    finally:
        if acquired:
            held.remove(directory)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def reserve_invocation(directory, run_id, stage, prompt, source=None, *, expected_cycle=None):
    """Reserve one unique stage record within an active shared run.

    Validate the assigned run and hold its cooperative lock while reading the
    current metadata, active cycle, and durable invocation counters. A cycle is
    active when its directory exists and its number is one more than the last
    completed cycle. Reject a stale expected cycle or a terminal run.

    For older runs without counters, recover the last sequence, previous record,
    and current-cycle stage counts from existing JSON records. Persist the next
    reservation atomically before returning its cycle, record name and provenance.
    A process that dies after reservation can leave a gap, but a later process
    never overwrites its reserved name or invents its missing result.
    """
    import json

    if not isinstance(stage, str) or re.fullmatch(r'[a-z][a-z0-9_]*', stage) is None:
        raise ValueError('Stage must be a lowercase stage name')
    with locked_assigned_run(directory, run_id) as run:
        metadata = yaml.safe_load((run / 'metadata.yaml').read_text())
        if metadata.get('status') != 'running':
            raise RuntimeError('Stage calls require an active run')
        cycle = metadata.get('completed_cycles', 0) + 1
        cycle_dir = run / 'state' / f'cycle_{cycle:06d}'
        if not cycle_dir.is_dir() or (expected_cycle is not None and expected_cycle != cycle):
            raise RuntimeError('Stage calls require the active cycle')
        state_path = run / 'state' / '.invocations.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if (type(state.get('sequence')) is not int or type(state.get('cycle')) is not int
                    or not isinstance(state.get('counts'), dict)):
                raise ValueError('Invalid invocation reservation state')
        else:
            state = dict(cycle=cycle, sequence=0, counts={}, previous_record=None)
            for path in sorted((run / 'state').glob('cycle_*/*.json')):
                record = json.loads(path.read_text())
                sequence = record.get('sequence', 0)
                if type(sequence) is int and sequence > state['sequence']:
                    state['sequence'] = sequence
                    state['previous_record'] = str(path.relative_to(run))
                if path.parent == cycle_dir and isinstance(record.get('stage'), str):
                    number = record.get('invocation', 0)
                    if type(number) is int:
                        stage_name = record['stage']
                        state['counts'][stage_name] = max(state['counts'].get(stage_name, 0), number)
        if state['cycle'] != cycle:
            if state['cycle'] > cycle:
                raise RuntimeError('Invocation state is ahead of the active cycle')
            state['cycle'], state['counts'] = cycle, {}
        count = state['counts'].get(stage, 0) + 1
        state['counts'][stage] = count
        state['sequence'] += 1
        name = f'{stage}.json' if count == 1 else f'{stage}_{count:03d}.json'
        provenance = dict(sequence=state['sequence'], invocation=count,
                          started_at=datetime.now().astimezone().isoformat(),
                          prompt_name=prompt, source_record=source,
                          previous_record=state['previous_record'])
        state['previous_record'] = str(Path('state') / cycle_dir.name / name)
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=state_path.parent,
                                prefix='.invocations-', delete=False) as output:
            pending = Path(output.name)
            try:
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
                pending.replace(state_path)
            finally:
                pending.unlink(missing_ok=True)
        return cycle, name, provenance
