from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import importlib.util
import json
from unittest.mock import MagicMock

import pytest
import yaml

from smpa_ros.loop import assigned_run, initialize_run, finalize_run, run



def model_snapshot(prompt, **settings):
    return '# World Model\n\n## Entities\n\n- observed'


def test_concurrent_sessions_are_isolated(tmp_path):
    with ThreadPoolExecutor(4) as pool:
        sessions = list(pool.map(lambda _: run(root=tmp_path, run_name='../safe name',
                                               sense=lambda directory, cycle: 'observation', model=model_snapshot), range(4)))
    assert len(set(sessions)) == 4
    identities = set()
    for directory in sessions:
        metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
        identities.add(metadata['run_id'])
        assert metadata['run_name'] == 'safe_name'
        for _ in range(3):
            assert assigned_run(directory, metadata['run_id']) == (directory, metadata['run_id'])
    assert len(identities) == 4
    assert len(list((tmp_path / 'runs').iterdir())) == 4


def test_new_run_leaves_existing_storage_untouched(tmp_path):
    legacy = tmp_path / 'legacy' / 'audio'
    legacy.mkdir(parents=True)
    (legacy / 'recording.wav').write_bytes(b'historical audio')
    (tmp_path / 'scene.md').write_text('existing observation')
    previous = run(root=tmp_path, sense=lambda directory, cycle: 'previous', model=model_snapshot)
    before = {path.relative_to(tmp_path): path.read_bytes()
              for path in tmp_path.rglob('*') if path.is_file()}
    current = run(root=tmp_path, sense=lambda directory, cycle: 'current', model=model_snapshot)
    assert current != previous
    assert all((tmp_path / path).read_bytes() == content for path, content in before.items())
    assert {path.name for path in tmp_path.iterdir()} == {'legacy', 'scene.md', 'runs'}


@pytest.mark.parametrize('limit', [1, 3])
def test_sequential_cycles_share_run(tmp_path, limit):
    calls = []
    assignments = []

    def sense(directory, cycle):
        assignments.append(directory)
        calls.append('sense')
        return f'observation {cycle}'

    def backend(name):
        result = MagicMock()
        def generate(prompt, **kwargs):
            calls.append(name)
            return f'{name}: {prompt}'
        result.generate.side_effect = generate
        return result

    options = {} if limit == 1 else {'max_cycles': limit}
    directory = run(root=tmp_path, sense=sense, model=backend('model'), plan=backend('plan'),
                    act=backend('act'), **options)
    assert calls == ['sense', 'model', 'plan', 'act'] * limit
    assert set(assignments) == {directory}
    assert list((tmp_path / 'runs').iterdir()) == [directory]
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert metadata['completed_cycles'] == limit and metadata['status'] == 'completed'
    assert len(list((directory / 'state').glob('cycle_*/*.json'))) == limit * 4


@pytest.mark.parametrize('stage', ['sense', 'model', 'plan', 'act'])
def test_failed_stage_stops_and_preserves_results(tmp_path, stage):
    calls = []
    def sense(directory, cycle):
        calls.append('sense')
        if stage == 'sense':
            raise RuntimeError('failed stage')
        return 'observation'
    def backend(name):
        result = MagicMock()
        def generate(prompt, **kwargs):
            calls.append(name)
            if name == stage:
                raise RuntimeError('failed stage')
            return name
        result.generate.side_effect = generate
        return result
    with pytest.raises(RuntimeError, match='failed stage'):
        run(3, root=tmp_path, sense=sense, **{name: backend(name) for name in ('model', 'plan', 'act')})
    names = ['sense', 'model', 'plan', 'act']
    assert calls == names[:names.index(stage) + 1]
    directory, = (tmp_path / 'runs').iterdir()
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert metadata['status'] == 'failed' and metadata['completed_cycles'] == 0
    assert len(list((directory / 'state').glob('cycle_*/*.json'))) == names.index(stage) + (stage == 'model')


def test_interruption_retains_completed_cycles(tmp_path):
    def sense(directory, cycle):
        (directory / 'observations' / 'vision' / f'{cycle}.md').write_text('captured')
        if cycle == 2:
            raise KeyboardInterrupt
        return 'observed'
    with pytest.raises(KeyboardInterrupt):
        run(3, root=tmp_path, sense=sense, model=model_snapshot)
    directory, = (tmp_path / 'runs').iterdir()
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert metadata['status'] == 'interrupted' and metadata['completed_cycles'] == 1
    assert len(list((directory / 'observations' / 'vision').iterdir())) == 2
    record = json.loads((directory / 'state/cycle_000001/act.json').read_text())
    assert record['status'] == 'placeholder'


@pytest.mark.parametrize('limit', [0, -1, True, 1.5])
def test_invalid_limit_creates_no_run(tmp_path, limit):
    with pytest.raises(ValueError, match='positive integer'):
        run(limit, root=tmp_path, sense=MagicMock())
    assert not (tmp_path / 'runs').exists()


def test_missing_or_mismatched_assignment(tmp_path):
    with pytest.raises(ValueError, match='Missing'):
        assigned_run('')
    with pytest.raises(ValueError, match='Invalid'):
        assigned_run(tmp_path)
    directory, metadata = initialize_run(tmp_path)
    with pytest.raises(ValueError, match='does not match'):
        assigned_run(directory, 'wrong')
    finalize_run(directory, metadata, 'completed')
    finalize_run(directory, metadata, 'failed')
    assert yaml.safe_load((directory / 'metadata.yaml').read_text())['status'] == 'completed'


@pytest.mark.parametrize('entrypoint', ['llm', 'speech'])
def test_launch_only_starts_loop(tmp_path, monkeypatch, entrypoint):
    from launch import LaunchDescription, LaunchService
    from launch.actions import ExecuteProcess, OpaqueFunction
    import launch_ros.actions
    import sys
    from ament_index_python.packages import get_package_share_directory
    source = Path(get_package_share_directory('smpa_ros')) / f'launch/{entrypoint}.launch.py'
    spec = importlib.util.spec_from_file_location('test_entrypoint', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assignments = []
    def fake_node(**kwargs):
        assert not (tmp_path / 'runs').exists()  # Launcher cannot initialize storage.
        assignments.append(kwargs)
        options = kwargs['arguments']
        root = options[options.index('--world-model-root') + 1]
        script = ('from smpa_ros.loop import run; '
                  f'run(3, root={root!r}, sense=lambda directory, cycle: "observed", model=lambda prompt, **kw: "# World Model")')
        return ExecuteProcess(cmd=[sys.executable, '-c', script], on_exit=kwargs['on_exit'])
    monkeypatch.setattr(launch_ros.actions, 'Node', fake_node)
    if entrypoint == 'llm':
        monkeypatch.setattr(module, 'Node', fake_node)
    def start(context):
        context.launch_configurations.update(world_model_root=str(tmp_path), max_cycles='3')
        return list(module.generate_launch_description().entities)
    launch = LaunchService()
    launch.include_launch_description(LaunchDescription([OpaqueFunction(function=start)]))
    launch.run()
    assert [item['executable'] for item in assignments] == ['loop']
    args = assignments[0]['arguments']
    assert args[args.index('--max-cycles') + 1] == '3'
    assert args[args.index('--service') + 1] == ('speech' if entrypoint == 'speech' else 'vision')
    directory, = (tmp_path / 'runs').iterdir()
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert metadata['status'] == 'completed' and metadata['completed_cycles'] == 3


def test_participant_restarts_share_run(assigned_test_run):
    from unittest.mock import MagicMock
    import rclpy
    from smpa_ros.sense.service import SenseService
    from smpa_ros.speech.actions import SpeechServer

    session = assigned_test_run
    original = (session.directory / 'metadata.yaml').read_bytes()
    rclpy.init(args=[])
    try:
        for _ in range(2):
            nodes = [SenseService(backend=MagicMock()),
                     SpeechServer('listen', backend=MagicMock()),
                     SpeechServer('talk', backend=MagicMock())]
            try:
                assert {node.run_id for node in nodes} == {session.run_id}
                assert {node.world_model_dir for node in nodes} == {session.directory}
            finally:
                for node in nodes:
                    if hasattr(node, 'server'):
                        node.server.destroy()
                    node.destroy_node()
        assert (session.directory / 'metadata.yaml').read_bytes() == original
        assert list(session.directory.parent.iterdir()) == [session.directory]
    finally:
        rclpy.shutdown()


def test_participant_launcher_consumes_one_assignment(assigned_test_run, monkeypatch):
    from launch import LaunchContext
    from ament_index_python.packages import get_package_share_directory
    source = Path(get_package_share_directory('smpa_ros')) / 'launch/llm.launch.py'
    spec = importlib.util.spec_from_file_location('test_participants', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'Node', lambda **kwargs: kwargs)
    session = assigned_test_run
    before = (session.directory / 'metadata.yaml').read_bytes()
    context = LaunchContext()
    context.launch_configurations.update(participants_only='true', service='all',
        world_model_dir=str(session.directory), run_id=session.run_id, python='python3',
        config='pipeline.yaml', model_root='/models', audio_lock='/tmp/test.lock',
        input_device='', output_device='')
    nodes = module.start(context)
    assert [node['executable'] for node in nodes] == ['sense', 'listen', 'talk']
    for node in nodes:
        assert node['parameters'][0]['run_id'] == session.run_id
        assert node['parameters'][0]['world_model_dir'] == str(session.directory)
        assert node['remappings'] == nodes[0]['remappings']
    assert (session.directory / 'metadata.yaml').read_bytes() == before
    assert list(session.directory.parent.iterdir()) == [session.directory]


def test_act_operation_runs_before_cycle_completion(tmp_path):
    calls = []
    def act(prompt, *, max_new_tokens):
        directory, = (tmp_path / 'runs').iterdir()
        metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
        assert metadata['completed_cycles'] == 0
        calls.append(prompt)
        return 'action result'
    directory = run(root=tmp_path, sense=lambda directory, cycle: 'observed', model=model_snapshot, act=act)
    assert len(calls) == 1 and 'observed' in calls[0]
    record = json.loads((directory / 'state/cycle_000001/act.json').read_text())
    assert record['status'] == 'generated' and record['output'] == 'action result'


def test_model_iterations_chain_and_feed_plan(tmp_path):
    snapshots = ['# World Model\n\n## Entities\n\n- cup_1: Cup',
                 '# World Model\n\n## Entities\n\n- cup_1: Cup\n- table_1: Table']
    backend = MagicMock()
    backend.generate.side_effect = snapshots
    plan = MagicMock()
    plan.generate.return_value = 'proposed plan'
    def sense(directory, cycle):
        (directory / 'observations/vision' / f'{cycle}.md').write_text(f'observation {cycle}')
        if cycle == 2:
            assert (directory / 'world_model/iteration_001.md').read_text() == snapshots[0]
        return f'observation {cycle}'
    directory = run(2, root=tmp_path, sense=sense, model=backend, plan=plan,
                    model_identifier='test/local-model')
    assert (directory / 'world_model/iteration_001.md').read_text() == snapshots[0]
    assert (directory / 'world_model/iteration_002.md').read_text() == snapshots[1]
    assert 'Previous world model:\n\n' in backend.generate.call_args_list[0].args[0]
    assert snapshots[0] in backend.generate.call_args_list[1].args[0]
    for cycle, snapshot in enumerate(snapshots, 1):
        assert snapshot in plan.generate.call_args_list[cycle - 1].args[0]
        record = json.loads((directory / f'state/cycle_{cycle:06d}/model.json').read_text())
        assert record['model_identifier'] == 'test/local-model'
        assert f'observations/vision/{cycle}.md' in record['source_observations']
        assert record['iteration'] == f'world_model/iteration_{cycle:03d}.md'
        assert record['previous_iteration'] == ('world_model/iteration_001.md' if cycle == 2 else None)


@pytest.mark.parametrize('failure', [RuntimeError('model failed'), '', '   '])
def test_model_failure_preserves_previous_snapshot(tmp_path, failure):
    backend = MagicMock()
    backend.generate.side_effect = ['# World Model\n\nFirst snapshot', failure]
    plan = MagicMock()
    plan.generate.return_value = 'plan'
    with pytest.raises((RuntimeError, ValueError)):
        run(2, root=tmp_path, sense=lambda directory, cycle: f'observation {cycle}', model=backend, plan=plan)
    directory, = (tmp_path / 'runs').iterdir()
    assert (directory / 'world_model/iteration_001.md').read_text() == '# World Model\n\nFirst snapshot'
    assert not (directory / 'world_model/iteration_002.md').exists()
    assert plan.generate.call_count == 1
    record = json.loads((directory / 'state/cycle_000002/model.json').read_text())
    assert record['status'] == 'failed' and record['error'] and record['iteration'] is None
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert metadata['status'] == 'failed' and metadata['completed_cycles'] == 1


def test_model_never_overwrites_existing_iteration(tmp_path):
    def sense(directory, cycle):
        (directory / 'world_model/iteration_001.md').write_text('preserve existing snapshot')
        return 'observation'
    with pytest.raises(FileExistsError):
        run(root=tmp_path, sense=sense, model=model_snapshot)
    directory, = (tmp_path / 'runs').iterdir()
    assert (directory / 'world_model/iteration_001.md').read_text() == 'preserve existing snapshot'
    assert not list((directory / 'world_model').glob('.iteration-*'))


def test_default_memory_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = run(sense=lambda directory, cycle: 'observation', model=model_snapshot)
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert directory.parent == tmp_path / 'memory/runs'
    assert directory.name == metadata['run_id']
    assert (directory / 'observations/vision').is_dir()
    assert (directory / 'observations/audio').is_dir()
    assert (directory / 'world_model/iteration_001.md').is_file()


def test_missing_model_backend_fails_explicitly(tmp_path):
    with pytest.raises(ValueError, match='configured local LLM'):
        run(root=tmp_path, sense=lambda directory, cycle: 'observation')
    directory, = (tmp_path / 'runs').iterdir()
    assert not list((directory / 'world_model').glob('iteration_*.md'))
    assert json.loads((directory / 'state/cycle_000001/model.json').read_text())['status'] == 'failed'
