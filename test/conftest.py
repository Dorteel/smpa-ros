"""Assign temporary loop-owned runs to operation-server tests."""
from types import SimpleNamespace
import pytest

from memory import MemoryManager
from smpa_ros.smpa import GOAL


@pytest.fixture(autouse=True)
def assigned_test_run(tmp_path_factory, monkeypatch):
    import rclpy
    memory = MemoryManager(tmp_path_factory.mktemp('assigned_runs'), 'test', goal=GOAL)
    directory, metadata = memory.directory, memory.metadata
    original = rclpy.init

    def init(*, args=None, **kwargs):
        args = list(args or []) + ['--ros-args', '-p', f'world_model_dir:={directory}',
                                   '-p', f'run_id:={metadata["run_id"]}']
        return original(args=args, **kwargs)

    monkeypatch.setattr(rclpy, 'init', init)
    yield SimpleNamespace(directory=directory, run_id=metadata['run_id'], metadata=metadata)
    memory.finalize('completed')


@pytest.fixture(autouse=True)
def injected_storage_sensors(request, monkeypatch):
    """Route injected storage-test sensors through the real Sense callback.

    The storage, camera and cognition regressions use this fixture. Their fake node owns the observation callable, keeping
    concurrent runs isolated. Real ROS nodes continue using ordinary sensing.
    """
    if request.module.__name__ not in ('test_run_storage', 'test_cognition', 'test_loop'):
        return
    import smpa_ros.sense_node as module
    original = module.observe
    def observe(node, configuration, directory, executor, timeout, service, speech, **options):
        """Use the test-owned observer only when this fake node supplies one."""
        if hasattr(node, 'test_observe'):
            cycle = max(int(path.name.split('_')[1]) for path in (directory / 'state').glob('cycle_*'))
            return node.test_observe(directory, cycle), ''
        return original(node, configuration, directory, executor, timeout, service, speech, **options)
    monkeypatch.setattr(module, 'observe', observe)
