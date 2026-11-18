"""Exercise the shared run identity and cooperative process lock without ROS."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from memory import MemoryManager, locked_run


def test_locked_run_validates_identity_and_releases_after_error(tmp_path):
    """Resolve the exact run and release its lock when an operation fails.

    Create an ordinary run using MemoryManager. Check that its ID selects its
    directory and malformed or unknown identities fail. An exception from a
    caller must not prevent another caller from acquiring the same run lock.
    """
    memory = MemoryManager(tmp_path, goal='Observe the scene')
    identity = memory.metadata['run_id']
    with pytest.raises(RuntimeError):
        with locked_run(tmp_path, identity) as directory:
            assert directory == memory.directory
            raise RuntimeError('fixture failure')
    with locked_run(tmp_path, identity) as directory:
        assert directory == memory.directory
    with pytest.raises(ValueError):
        with locked_run(tmp_path, '../other'):
            pass
    with pytest.raises(ValueError):
        with locked_run(tmp_path, '00000000-0000-0000-0000-000000000000'):
            pass


def test_locked_run_serializes_independent_callers(tmp_path):
    """Keep a second writer outside the run until the first writer releases it.

    Use two threads with separate lock descriptors to model independent users of
    the same run. The second caller signals when it starts and when it acquires
    the lock. It must acquire only after the first caller leaves its context.
    """
    memory = MemoryManager(tmp_path, goal='Observe the scene')
    identity = memory.metadata['run_id']
    waiting, acquired = Event(), Event()

    def second_writer():
        """Signal the attempted write and acquire the same run lock afterward.

        This function does not modify the run; the events expose the ordering
        enforced by the two independently opened advisory lock descriptors.
        """
        waiting.set()
        with locked_run(tmp_path, identity):
            acquired.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with locked_run(tmp_path, identity):
            future = pool.submit(second_writer)
            assert waiting.wait(2)
            assert not acquired.wait(0.1)
        future.result(timeout=2)
    assert acquired.is_set()


def test_locked_run_serializes_a_separate_process(tmp_path):
    """Keep a child process from entering a run while the parent holds its lock.

    Start a fresh Python interpreter that announces its attempt using a pipe,
    then requests the same run. A nonblocking readiness check must show no
    acquisition until the parent releases its lock. The child must then finish
    normally and report the acquisition without changing experiment records.
    """
    import select
    import subprocess
    import sys

    memory = MemoryManager(tmp_path, goal='Observe the scene')
    code = (
        'import sys\n'
        'from memory import locked_run\n'
        'print("waiting", flush=True)\n'
        'with locked_run(sys.argv[1], sys.argv[2]):\n'
        '    print("acquired", flush=True)\n'
    )
    with locked_run(tmp_path, memory.metadata['run_id']):
        child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path),
                                  memory.metadata['run_id']], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == 'waiting'
            ready, _, _ = select.select([child.stdout], [], [], 0.1)
            assert not ready
        except BaseException:
            child.kill()
            child.wait(timeout=2)
            raise
    stdout, stderr = child.communicate(timeout=2)
    assert child.returncode == 0, stderr
    assert stdout.strip() == 'acquired'


def test_metadata_writers_merge_independent_fields_and_detect_conflicts(tmp_path):
    """Preserve independent edits from stale managers and reject conflicting edits.

    Give two manager instances the same previously read metadata snapshot. Write
    distinct nested fields in turn and check that both survive. Then attempt two
    different updates to the same field and require the second writer to fail
    without replacing the first writer's durable value.
    """
    from copy import deepcopy
    import yaml

    first = MemoryManager(tmp_path, goal='Observe the scene')
    second = MemoryManager.__new__(MemoryManager)
    second.directory = first.directory
    second.metadata = deepcopy(first.metadata)
    second._metadata_snapshot = deepcopy(first.metadata)
    first.metadata['configuration']['first'] = 'one'
    second.metadata['configuration']['second'] = 'two'
    first.write_metadata()
    second.write_metadata()
    stored = yaml.safe_load((first.directory / 'metadata.yaml').read_text())
    assert stored['configuration'] == {'first': 'one', 'second': 'two'}
    first.metadata['status'] = 'failed'
    second.metadata['status'] = 'completed'
    first.write_metadata()
    with pytest.raises(RuntimeError, match='Concurrent run metadata'):
        second.write_metadata()
    stored = yaml.safe_load((first.directory / 'metadata.yaml').read_text())
    assert stored['status'] == 'failed'
    assert stored['configuration'] == {'first': 'one', 'second': 'two'}


def test_metadata_writers_in_separate_processes_preserve_both_updates(tmp_path):
    """Combine separate process updates derived from the same old metadata.

    Each child loads the same snapshot and prepares a distinct configuration
    field. Release both writers together, then verify neither update was lost
    and that the metadata remains valid YAML with the original run identity.
    """
    import subprocess
    import sys
    import time
    import yaml

    memory = MemoryManager(tmp_path, goal='Observe the scene')
    start = tmp_path / 'start'
    code = (
        'from pathlib import Path\n'
        'from copy import deepcopy\n'
        'from time import monotonic, sleep\n'
        'import sys, yaml\n'
        'from memory import MemoryManager\n'
        'directory, key, start, ready = map(Path, sys.argv[1:])\n'
        'metadata = yaml.safe_load((directory / "metadata.yaml").read_text())\n'
        'manager = MemoryManager.__new__(MemoryManager)\n'
        'manager.directory, manager.metadata = directory, metadata\n'
        'manager._metadata_snapshot = deepcopy(metadata)\n'
        'manager.metadata["configuration"][key.name] = key.name\n'
        'ready.touch()\n'
        'deadline = monotonic() + 5\n'
        'while not start.exists() and monotonic() < deadline: sleep(0.01)\n'
        'assert start.exists()\n'
        'manager.write_metadata()\n'
    )
    children = [subprocess.Popen([sys.executable, '-c', code, str(memory.directory),
                                  name, str(start), str(tmp_path / (name + '.ready'))],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for name in ('first', 'second')]
    try:
        deadline = time.monotonic() + 5
        while not all((tmp_path / (name + '.ready')).exists() for name in ('first', 'second')):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        start.touch()
        for child in children:
            _, error = child.communicate(timeout=5)
            assert child.returncode == 0, error
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
    stored = yaml.safe_load((memory.directory / 'metadata.yaml').read_text())
    assert stored['run_id'] == memory.metadata['run_id']
    assert stored['configuration'] == {'first': 'first', 'second': 'second'}


def test_dead_process_does_not_keep_run_locked(tmp_path):
    """Recover the run lock after a process dies while holding it.

    Wait until a child has acquired the run lock, terminate that child, and then
    acquire the same lock in the parent. Advisory locks must not leave a stale
    lock that prevents later writers from accessing the original run.
    """
    import subprocess
    import sys

    memory = MemoryManager(tmp_path, goal='Observe the scene')
    code = ('import sys, time\nfrom memory import locked_run\n'
            'with locked_run(sys.argv[1], sys.argv[2]):\n'
            '    print("locked", flush=True)\n    time.sleep(30)\n')
    child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path), memory.metadata['run_id']],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
    finally:
        child.kill()
        child.wait(timeout=2)
    with locked_run(tmp_path, memory.metadata['run_id']) as directory:
        assert directory == memory.directory


def test_nested_metadata_write_uses_outer_run_lock(tmp_path):
    """Allow a run transaction to publish metadata without reacquiring its lock.

    Hold the public lock while the same thread updates metadata. The update must
    complete within the outer transaction and be visible to a later acquisition.
    """
    import yaml

    memory = MemoryManager(tmp_path, goal='Observe the scene')
    with locked_run(tmp_path, memory.metadata['run_id']):
        memory.metadata['configuration']['owner'] = 'model'
        memory.write_metadata()
    with locked_run(tmp_path, memory.metadata['run_id']) as directory:
        assert yaml.safe_load((directory / 'metadata.yaml').read_text())['configuration']['owner'] == 'model'


def test_metadata_updates_work_for_an_assigned_run_outside_runs_directory(tmp_path):
    """Preserve the existing ability to update an assigned run at another path.

    Move a newly created run to a directory without a runs parent, as an existing
    assigned conversation may be stored elsewhere. A metadata update must still
    use its actual directory and retain its identity and historical files.
    """
    import yaml

    memory = MemoryManager(tmp_path, goal='Observe the scene')
    memory.directory = memory.directory.rename(tmp_path / 'assigned-run')
    memory.metadata['configuration']['assigned'] = True
    memory.write_metadata()
    stored = yaml.safe_load((memory.directory / 'metadata.yaml').read_text())
    assert stored['run_id'] == memory.metadata['run_id']
    assert stored['configuration']['assigned'] is True


def test_separate_processes_reserve_unique_stage_invocations(tmp_path):
    """Allocate distinct record names and provenance for concurrent stage calls.

    Start two separate processes for the same run and active cycle. Release them
    together and verify that both receive different ordered names and sequence
    numbers. A later allocation must continue after both reservations even when
    neither worker has written a stage result, as after an interrupted worker.
    """
    import json
    import subprocess
    import sys
    import time
    from memory import reserve_invocation

    memory = MemoryManager(tmp_path, goal='Observe the scene')
    memory.begin_cycle()
    start = tmp_path / 'start'
    code = (
        'from pathlib import Path\n'
        'from time import monotonic, sleep\n'
        'import json, sys\n'
        'from memory import reserve_invocation\n'
        'directory, identity, start, ready = sys.argv[1:]\n'
        'Path(ready).touch()\n'
        'deadline = monotonic() + 5\n'
        'while not Path(start).exists() and monotonic() < deadline: sleep(0.01)\n'
        'assert Path(start).exists()\n'
        'print(json.dumps(reserve_invocation(directory, identity, "model", "default")))\n'
    )
    children = [subprocess.Popen([sys.executable, '-c', code, str(memory.directory),
                                  memory.metadata['run_id'], str(start), str(tmp_path / f'{n}.ready')],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for n in (1, 2)]
    try:
        deadline = time.monotonic() + 5
        while not all((tmp_path / f'{n}.ready').exists() for n in (1, 2)):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        start.touch()
        results = []
        for child in children:
            output, error = child.communicate(timeout=5)
            assert child.returncode == 0, error
            results.append(json.loads(output))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
    assert {result[1] for result in results} == {'model.json', 'model_002.json'}
    assert {result[2]['sequence'] for result in results} == {1, 2}
    cycle, name, provenance = reserve_invocation(memory.directory, memory.metadata['run_id'], 'plan', 'default')
    assert (cycle, name, provenance['sequence']) == (1, 'plan.json', 3)
    assert provenance['previous_record'] == 'state/cycle_000001/model_002.json'
