"""Verify world-model writes using independent processes and mocked inference."""
import json
import subprocess
import sys
import time

from memory import MemoryManager


def test_world_model_writers_serialize_and_preserve_both_snapshots(tmp_path):
    """Keep two processes from generating and publishing the same run at once.

    Give each worker a distinct reserved record name and a fake inference
    callable that records entry and exit. Both snapshots and provenance records
    must be retained, and the trace must show that the two model operations did
    not overlap. No inference backend or external ROS service is started.
    """
    memory = MemoryManager(tmp_path, goal='Observe the scene')
    memory.begin_cycle()
    go = tmp_path / 'go'
    trace = tmp_path / 'trace'
    code = '''
from pathlib import Path
from time import monotonic, sleep
import os, sys
from smpa_ros.model import generate_world_model
run, name, start, ready, trace = map(Path, sys.argv[1:])
def generate(prompt, **settings):
    descriptor = os.open(trace, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, b'start\\n')
        sleep(0.15)
        os.write(descriptor, b'end\\n')
    finally:
        os.close(descriptor)
    return '# World Model\\n\\nA visible cup.'
ready.touch()
deadline = monotonic() + 5
while not start.exists() and monotonic() < deadline: sleep(0.01)
assert start.exists()
generate_world_model(run, 1, 'visible cup', generate, observations=[], pipeline={},
                     record_name=name.name, previous_snapshot='',
                     provenance={'sequence': 1 if name.name == 'model.json' else 2})
'''
    children = [subprocess.Popen([sys.executable, '-c', code, str(memory.directory), name,
                                  str(go), str(tmp_path / f'{index}.ready'), str(trace)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for index, name in enumerate(('model.json', 'model_002.json'))]
    try:
        deadline = time.monotonic() + 8
        while not all((tmp_path / f'{index}.ready').exists() for index in (0, 1)):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        go.touch()
        for child in children:
            _, error = child.communicate(timeout=8)
            assert child.returncode == 0, error
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
    assert trace.read_text().splitlines() == ['start', 'end', 'start', 'end']
    for name, snapshot in [('model.json', 'iteration_001.md'),
                           ('model_002.json', 'iteration_001_002.md')]:
        record = json.loads((memory.directory / 'state/cycle_000001' / name).read_text())
        assert record['status'] == 'generated'
        assert (memory.directory / 'world_model' / snapshot).is_file()


def test_world_model_failure_releases_lock_and_keeps_prior_record(tmp_path):
    """Permit a later model operation after a failed inference attempt.

    Persist the first invocation's failure record, then run a distinct invocation
    with a successful fake model. Verify the original failure remains intact and
    the later snapshot is published without reusing its record name.
    """
    import pytest
    from smpa_ros.model import generate_world_model

    memory = MemoryManager(tmp_path, goal='Observe the scene')
    memory.begin_cycle()

    def fail(prompt, **settings):
        """Raise a controlled failure without accessing an inference backend."""
        raise RuntimeError('fixture inference failure')

    def succeed(prompt, **settings):
        """Return a fixed world model without loading a model instance."""
        return '# World Model\n\nA visible cup.'

    with pytest.raises(RuntimeError, match='fixture inference failure'):
        generate_world_model(memory.directory, 1, 'cup', fail, observations=[], pipeline={},
                             record_name='model.json', previous_snapshot='')
    generate_world_model(memory.directory, 1, 'cup', succeed, observations=[], pipeline={},
                         record_name='model_002.json', previous_snapshot='')
    state = memory.directory / 'state/cycle_000001'
    assert json.loads((state / 'model.json').read_text())['status'] == 'failed'
    assert json.loads((state / 'model_002.json').read_text())['status'] == 'generated'
    assert (memory.directory / 'world_model/iteration_001_002.md').is_file()
