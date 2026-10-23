"""Assign temporary loop-owned runs to operation-server tests."""
from types import SimpleNamespace
import pytest

from smpa_ros.loop import initialize_run, finalize_run


@pytest.fixture(autouse=True)
def assigned_test_run(tmp_path_factory, monkeypatch):
    import rclpy
    directory, metadata = initialize_run(tmp_path_factory.mktemp('assigned_runs'), 'test')
    original = rclpy.init

    def init(*, args=None, **kwargs):
        args = list(args or []) + ['--ros-args', '-p', f'world_model_dir:={directory}',
                                   '-p', f'run_id:={metadata["run_id"]}']
        return original(args=args, **kwargs)

    monkeypatch.setattr(rclpy, 'init', init)
    yield SimpleNamespace(directory=directory, run_id=metadata['run_id'], metadata=metadata)
    finalize_run(directory, metadata, 'completed')
