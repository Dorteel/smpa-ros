from unittest.mock import MagicMock

import pytest
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from smpa_ros.loop import capture_once
from smpa_ros.sense.service import SenseService


@pytest.mark.parametrize('fails', [False, True])
def test_camera_sense_markdown(tmp_path, fails, assigned_test_run):
    """Only inference is mocked; camera messages and service calls use ROS."""
    run_dir = assigned_test_run.directory
    rclpy.init(args=[])
    executor = SingleThreadedExecutor()
    camera = loop = server = None
    try:
        backend = MagicMock()
        text = '# Observation\n\nA red pixel.\n'
        backend.describe.return_value = text
        if fails:
            backend.describe.side_effect = RuntimeError('model failed')
        server = SenseService(backend=backend)
        camera = rclpy.create_node('test_camera')
        loop = rclpy.create_node('test_loop')
        for node in (server, camera, loop):
            executor.add_node(node)
        topic = '/test/smpa_camera'
        publisher = camera.create_publisher(Image, topic, qos_profile_sensor_data)
        image = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
        camera.create_timer(0.02, lambda: publisher.publish(image))
        config = {'camera_topic': topic, 'model': 'smolvlm2', 'prompt': 'describe_scene'}
        if fails:
            with pytest.raises(RuntimeError, match='model failed'):
                capture_once(loop, config, run_dir, executor, timeout=5)
            assert not list((run_dir / 'observations' / 'vision').glob('*.md'))
        else:
            path = capture_once(loop, config, run_dir, executor, timeout=5)
            assert path.name.endswith('_sense_output.md')
            assert path.read_bytes() == text.encode('utf-8')
            assert list((run_dir / 'observations' / 'vision').glob('*.md')) == [path]
        backend.describe.assert_called_once()
        assert backend.describe.call_args.args[1] == 'Describe the scene.'
    finally:
        executor.shutdown()
        for node in (loop, camera, server):
            if node is not None:
                node.destroy_node()
        rclpy.shutdown()


@pytest.mark.parametrize('configured, override, expected', [(None, None, 1), (3, None, 3), (3, 1, 1)])
def test_main_owns_run_and_uses_cycle_limit(tmp_path, monkeypatch, configured, override, expected):
    """Exercise main, real ROS image/service exchange and persistence; mock model/process creation."""
    import json
    import yaml
    import smpa_ros.loop as module
    from rclpy.qos import qos_profile_sensor_data

    config = {'camera_topic': '/test/smpa_main_camera', 'model': 'smolvlm2', 'prompt': 'describe_scene'}
    config['modeling'] = {'model': 'qwen3-0.6b'}
    model_backend = MagicMock()
    model_backend.generate.side_effect = lambda prompt, **settings: (
        '{"action":"look_left"}' if 'schema' in settings else '# World Model\n\n## Entities\n\n- pixel_1: Red pixel')
    def execute(node, executor, action, record, timeout):
        record.update(goal_accepted=True, result_status=4, success=True)
    monkeypatch.setattr(module, 'execute_action', execute)
    constructor = MagicMock(return_value=model_backend)
    monkeypatch.setattr('smpa_ros.backends.transformers.TransformersBackend', constructor)
    if configured is not None:
        config['execution'] = {'max_cycles': configured}
    config_path = tmp_path / 'pipeline.yaml'
    config_path.write_text(yaml.safe_dump(config))
    commands = []
    process = MagicMock()
    process.poll.return_value = None
    process.pid = 12345

    def launch(command, **kwargs):
        commands.append(command)
        return process

    def node(name):
        backend = MagicMock()
        backend.describe.return_value = 'A red pixel.'
        server = SenseService(backend=backend)
        publisher = server.create_publisher(Image, config['camera_topic'], qos_profile_sensor_data)
        image = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
        server.create_timer(0.02, lambda: publisher.publish(image))
        return server

    monkeypatch.setattr(module.subprocess, 'Popen', launch)
    monkeypatch.setattr(module.os, 'killpg', MagicMock())
    monkeypatch.setattr(rclpy, 'create_node', node)
    args = ['loop', '--config', str(config_path), '--world-model-root', str(tmp_path / 'world_model'),
            '--timeout', '5']
    if override is not None:
        args += ['--max-cycles', str(override)]
    module.main(args)
    directory, = (tmp_path / 'world_model/runs').iterdir()
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert metadata['completed_cycles'] == expected and metadata['status'] == 'completed'
    assert len(list((directory / 'observations' / 'vision').iterdir())) == expected
    assert len(list((directory / 'state').iterdir())) == expected
    constructor.assert_called_once()
    assert model_backend.generate.call_count == expected * 2
    assert len(list((directory / 'world_model').glob('iteration_*.md'))) == expected
    assert len(commands) == 1
    assert f'world_model_dir:={directory}' in commands[0]
    assert f'run_id:={metadata["run_id"]}' in commands[0]
    assert 'participants_only:=true' in commands[0]
    for cycle in range(1, expected + 1):
        assert json.loads((directory / f'state/cycle_{cycle:06d}/act.json').read_text())['status'] == 'succeeded'
