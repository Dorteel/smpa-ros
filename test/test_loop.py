from unittest.mock import MagicMock
from datetime import datetime
from pathlib import Path

from PIL import Image as PILImage

import pytest
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from smpa_ros.sense import capture_once
from smpa_ros.sense import SenseService


@pytest.mark.parametrize('fails', [False, True])
def test_camera_sense_markdown(tmp_path, fails, assigned_test_run):
    """Capture one original frame and its exact description through real ROS.

    Only inference is mocked. On success require one UTC-stamped Markdown/PNG
    pair in the assigned run, with unchanged pixels and text. On inference
    failure retain the original PNG and its camera metadata without writing a
    fabricated description, leaving run ownership unchanged.
    """
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
            images = list((run_dir / 'observations' / 'vision').glob('*.png'))
            assert len(images) == 1
            assert not list((run_dir / 'observations' / 'vision').glob('*.md'))
            with PILImage.open(images[0]) as evidence:
                assert evidence.getpixel((0, 0)) == (255, 0, 0)
                assert 'smpa.camera' in evidence.info
        else:
            from smpa_ros.sense import record_observation
            captured = {}

            def capture(directory, cycle):
                """Persist the one capture through the existing Sense record owner."""
                captured['path'] = capture_once(loop, config, directory, executor, timeout=5)
                return captured['path'].read_text()

            value, sources = record_observation(run_dir, 1, capture)
            path = captured['path']
            assert value == text and len(sources) == 2
            assert set(sources) == {str(path.relative_to(run_dir)),
                                    str(path.with_suffix('.png').relative_to(run_dir))}
            assert len(list((run_dir / 'state').glob('*/sense*.json'))) == 1
            assert path.name.endswith('_sense_output.md')
            assert path.read_bytes() == text.encode('utf-8')
            assert list((run_dir / 'observations' / 'vision').glob('*.md')) == [path]
            with PILImage.open(path.with_suffix('.png')) as saved:
                assert saved.size == (1, 1) and saved.mode == 'RGB'
                assert saved.getpixel((0, 0)) == (255, 0, 0)
            datetime.strptime(path.stem.removesuffix('_sense_output'), '%Y-%m-%d_%H-%M-%S_%fZ')
            assert set(path.parent.iterdir()) == {path, path.with_suffix('.png')}
        backend.describe.assert_called_once()
        assert backend.describe.call_args.args[1] == 'Describe the scene.'
    finally:
        executor.shutdown()
        for node in (loop, camera, server):
            if node is not None:
                node.destroy_node()
        rclpy.shutdown()


@pytest.mark.parametrize('backend_kind', ['local', 'api'])
@pytest.mark.parametrize('configured, override, expected', [(None, None, 1), (3, None, 3), (3, 1, 1)])
def test_main_owns_run_and_uses_cycle_limit(tmp_path, monkeypatch, configured, override, expected, backend_kind):
    """Retain cycle limits and camera evidence through independent node callbacks.

    Resolve native experiment arguments and use the shared mock backend for the
    existing camera service, Model and Plan. Run the actual script sequence via
    mocked stage clients. The real ROS image exchange and all original storage,
    source-link, runtime and action-result assertions remain in place.
    """
    import json
    import yaml
    from types import SimpleNamespace
    from test_run_storage import run
    from smpa_ros.smpa import configure_experiment, resolve_experiment_models
    config = {'camera_topic': '/test/smpa_main_camera', 'model': 'smolvlm2', 'prompt': 'describe_scene',
              'modeling': {'model': 'qwen3-0.6b'}}
    backend = MagicMock()
    backend.generate.side_effect = lambda prompt, **settings: (
        '{"action":"look_left"}' if 'schema' in settings else '# World Model\n\n## Entities\n\n- pixel_1: Red pixel')
    backend.describe.return_value = 'A red pixel.'
    constructor = MagicMock(side_effect=AssertionError('A stage must not load another model'))
    monkeypatch.setattr('smpa_ros.backends.transformers.TransformersVLM', constructor)
    monkeypatch.setattr('smpa_ros.model.resolve_vision_model', lambda *args, **kwargs: dict(
        backend='transformers', path='/local/vlm', model_id='test/vlm'))
    if backend_kind == 'api':
        config['model'] = dict(backend='api', provider='test', model_id='shared/vision-model')
        config['test'] = {'models': {'vision': 'shared/vision-model'}}
    if configured is not None:
        config['execution'] = {'max_cycles': configured}
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump(config))
    args = ['smpa', '--config', str(config_path), '--world-model-root', str(tmp_path / 'world_model'), '--timeout', '5']
    if override is not None:
        args += ['--max-cycles', str(override)]
    profile = SimpleNamespace()
    configure_experiment(profile, args)
    resolve_experiment_models(profile)
    rclpy.init(args=[])
    executor = SingleThreadedExecutor()
    service = SenseService(backend=backend)
    camera = rclpy.create_node('test_main_camera')
    capture_node = rclpy.create_node('test_main_capture')
    for node in (service, camera, capture_node):
        executor.add_node(node)
    publisher = camera.create_publisher(Image, config['camera_topic'], qos_profile_sensor_data)
    image = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
    camera.create_timer(0.02, lambda: publisher.publish(image))
    def sense(directory, cycle):
        """Capture real ROS pixels through the existing mocked vision service."""
        return capture_once(capture_node, config, directory, executor, timeout=5).read_text()
    runtime = dict(device='cuda:0', dtype='torch.float16') if backend_kind == 'local' else None
    try:
        directory = run(profile.limit, root=tmp_path / 'world_model', sense=sense, model=backend, plan=backend,
                        models=profile.models, configuration={'pipeline': profile.config, 'max_cycles': profile.limit},
                        runtime=runtime)
    finally:
        executor.shutdown()
        for node in (capture_node, camera, service):
            node.destroy_node()
        rclpy.shutdown()
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert metadata['completed_cycles'] == expected and metadata['status'] == 'completed'
    parameters = yaml.safe_load((directory / 'parameters.yaml').read_text())
    assert parameters.get('execution', {}).get('max_cycles', 1) == (configured or 1)
    assert parameters['_run']['max_cycles'] == expected
    observations = directory / 'observations' / 'vision'
    descriptions = list(observations.glob('*.md'))
    assert len(descriptions) == expected
    assert set(observations.iterdir()) == {file for description in descriptions for file in (description, description.with_suffix('.png'))}
    assert len(list((directory / 'state').glob('cycle_*'))) == expected
    constructor.assert_not_called()
    assert service.backend is backend
    assert backend.generate.call_count == expected * 2
    assert len(list((directory / 'world_model').glob('iteration_*.md'))) == expected
    assert backend.describe.call_count == expected
    if backend_kind == 'local':
        assert metadata['configuration']['runtime']['device'] == 'cuda:0'
    else:
        assert metadata['models']['vision']['model_id'] == metadata['models']['model']['model_id'] == 'shared/vision-model'
    for cycle in range(1, expected + 1):
        state = directory / f'state/cycle_{cycle:06d}'
        assert len(list(state.glob('sense*.json'))) == 1
        model = json.loads((state / 'model.json').read_text())
        assert len(model['source_observations']) == 3
        assert model['source_observations'][0] == f'state/cycle_{cycle:06d}/sense.json'
        assert {Path(name).suffix for name in model['source_observations'][1:]} == {'.md', '.png'}
        assert json.loads((state / 'act.json').read_text())['status'] == 'succeeded'



@pytest.mark.parametrize('entry_point', ['source', 'installed'])
def test_experiment_configuration_from_source_and_install(tmp_path, entry_point):
    """Resolve experiment configuration in a fresh source or installed process.

    The process uses a standalone configuration file and prepares run metadata
    without starting a stage, model client or sensor. Source and installed paths
    are checked separately so a stale installation cannot hide behind the checkout.
    """
    import os
    from pathlib import Path
    import subprocess
    import sys
    import uuid
    import yaml

    repository = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((repository / 'config.yaml').read_text())
    config['camera_topic'] = '/test/config_' + uuid.uuid4().hex
    config['model'] = dict(backend='api', provider='openai', model_id=config['openai']['models']['vision'])
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump(config))
    environment = os.environ.copy()
    if entry_point == 'source':
        environment['PYTHONPATH'] = str(repository / 'smpa_ros') + os.pathsep + str(repository / 'smpa_llm') + os.pathsep + environment.get('PYTHONPATH', '')
    else:
        environment['PYTHONPATH'] = os.pathsep.join(
            path for path in environment.get('PYTHONPATH', '').split(os.pathsep)
            if path and not Path(path).resolve().is_relative_to(repository))
    command = [sys.executable, '-c',
               'from smpa_ros.smpa import prepare_experiment; prepare_experiment(None, "# Test goal")']
    result = subprocess.run(
        command + ['--config', str(config_path), '--max-cycles', '1', '--timeout', '5',
                   '--python', sys.executable, '--world-model-root', str(tmp_path / 'memory')],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    directory, = (tmp_path / 'memory/runs').iterdir()
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    from smpa_ros.model import unify_model
    from smpa_ros.plan import enabled_actions
    expected = unify_model(config)
    expected['enabled_actions'] = list(enabled_actions(config))
    assert metadata['configuration']['pipeline'] == expected
    assert metadata['status'] == 'running' and (directory / 'goal.md').read_text() == '# Test goal'
