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
