"""Run directories, assignment validation, and lifecycle metadata."""
from datetime import datetime
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from uuid import uuid4

import yaml


class MemoryManager:
    """Own one run's identity and metadata without owning stage artifacts."""

    def __init__(self, root, run_name='smpa', *, models=None, configuration=None, goal):
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
        self.cycle = 0
        self.in_cycle = False
        self.counts = {}
        self.sequence = 0
        self.previous_record = None
        self.directory = directory
        self.metadata = metadata
        self.write_metadata()

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
        with NamedTemporaryFile(mode='w', dir=self.directory, prefix='.metadata-', delete=False) as output:
            pending = Path(output.name)
            try:
                yaml.safe_dump(self.metadata, output, sort_keys=False)
                output.flush()
                os.fsync(output.fileno())
                pending.replace(self.directory / 'metadata.yaml')
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
