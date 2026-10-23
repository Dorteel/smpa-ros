from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import importlib.util
import json
from unittest.mock import MagicMock

import pytest
import yaml

from smpa_ros.loop import assigned_run, initialize_run, finalize_run, run as run_session



def planned_action(prompt, **settings):
    return '{"action": "look_left"}'


def completed_action(action, record):
    record.update(goal_accepted=True, result_status=4, success=True)


def run(*args, **kwargs):
    kwargs.setdefault('plan', planned_action)
    kwargs.setdefault('act', completed_action)
    return run_session(*args, **kwargs)


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
            return planned_action(prompt) if name == 'plan' else f'{name}: {prompt}'
        result.generate.side_effect = generate
        return result

    def act(action, record):
        calls.append('act')
        completed_action(action, record)

    options = {} if limit == 1 else {'max_cycles': limit}
    directory = run(root=tmp_path, sense=sense, model=backend('model'), plan=backend('plan'),
                    act=act, **options)
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
            return planned_action(prompt) if name == 'plan' else name
        result.generate.side_effect = generate
        return result
    def act(action, record):
        calls.append('act')
        if stage == 'act':
            raise RuntimeError('failed stage')
        completed_action(action, record)
    with pytest.raises(RuntimeError, match='failed stage'):
        run(3, root=tmp_path, sense=sense, model=backend('model'), plan=backend('plan'), act=act)
    names = ['sense', 'model', 'plan', 'act']
    assert calls == names[:names.index(stage) + 1]
    directory, = (tmp_path / 'runs').iterdir()
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert metadata['status'] == 'failed' and metadata['completed_cycles'] == 0
    assert len(list((directory / 'state').glob('cycle_*/*.json'))) == names.index(stage) + (stage != 'sense')


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
    assert record['status'] == 'succeeded'


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
                  f'run(3, root={root!r}, sense=lambda directory, cycle: "observed", model=lambda prompt, **kw: "# World Model", '
                  f'plan=lambda prompt, **kw: ' + repr('{"action":"look_left"}') + ', '
                  'act=lambda action, record: record.update(goal_accepted=True, result_status=4, success=True))')
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
    def act(action, record):
        directory, = (tmp_path / 'runs').iterdir()
        metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
        assert metadata['completed_cycles'] == 0
        calls.append(action)
        completed_action(action, record)
    directory = run(root=tmp_path, sense=lambda directory, cycle: 'observed', model=model_snapshot, act=act)
    assert calls == ['look_left']
    record = json.loads((directory / 'state/cycle_000001/act.json').read_text())
    assert record['status'] == 'succeeded' and record['success'] is True


def test_model_iterations_chain_and_feed_plan(tmp_path):
    snapshots = ['# World Model\n\n## Entities\n\n- cup_1: Cup',
                 '# World Model\n\n## Entities\n\n- cup_1: Cup\n- table_1: Table']
    backend = MagicMock()
    backend.generate.side_effect = snapshots
    plan = MagicMock()
    plan.generate.return_value = planned_action('')
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
    plan.generate.return_value = planned_action('')
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
    assert directory.name.endswith('_smpa_' + metadata['run_id'])
    assert directory.name[:10] == metadata['started_at'][:10]
    assert (directory / 'observations/vision').is_dir()
    assert (directory / 'observations/audio').is_dir()
    assert (directory / 'world_model/iteration_001.md').is_file()


def test_missing_model_backend_fails_explicitly(tmp_path):
    with pytest.raises(ValueError, match='configured local LLM'):
        run(root=tmp_path, sense=lambda directory, cycle: 'observation')
    directory, = (tmp_path / 'runs').iterdir()
    assert not list((directory / 'world_model').glob('iteration_*.md'))
    assert json.loads((directory / 'state/cycle_000001/model.json').read_text())['status'] == 'failed'


@pytest.mark.parametrize('text', ['{"action":"drive"}', '{"action":"look_left","extra":true}', '{}', 'not JSON', 42])
def test_invalid_planner_decision_never_executes(tmp_path, text):
    action = MagicMock()
    with pytest.raises((ValueError, json.JSONDecodeError)):
        run(root=tmp_path, sense=lambda directory, cycle: 'seen', model=model_snapshot,
            plan=lambda prompt, **settings: text, act=action)
    action.assert_not_called()
    directory, = (tmp_path / 'runs').iterdir()
    assert json.loads((directory / 'state/cycle_000001/plan.json').read_text())['status'] == 'failed'
    assert (directory / 'world_model/iteration_001.md').is_file()
    assert not (directory / 'state/cycle_000001/act.json').exists()
    assert yaml.safe_load((directory / 'metadata.yaml').read_text())['status'] == 'failed'


def test_goal_file_is_the_planning_source(tmp_path):
    goal = '# Goal\n\nInspect the left side.'
    planned = MagicMock()
    planned.generate.return_value = '{"action":"look_left"}'
    directory = run(root=tmp_path, run_name='first test', goal=goal,
                    sense=lambda directory, cycle: 'seen', model=model_snapshot, plan=planned)
    assert (directory / 'goal.md').read_text() == goal
    assert '_first_test_' in directory.name
    assert directory.name[:10] == yaml.safe_load((directory / 'metadata.yaml').read_text())['started_at'][:10]
    record = json.loads((directory / 'state/cycle_000001/plan.json').read_text())
    assert record['goal'] == goal
    assert goal in record['prompt']
    assert 'look_left' in record['prompt'] and 'look_right' in record['prompt']
    from smpa_ros.loop import ACTION_SCHEMA
    assert planned.generate.call_args.kwargs['schema'] == ACTION_SCHEMA


@pytest.mark.parametrize('mode', ['success', 'rejected', 'aborted', 'timeout', 'unavailable'])
def test_ros_action_dispatch_and_cycle_order(tmp_path, monkeypatch, mode):
    """Real rclpy round trip; substitute Listen for unavailable simulation Look and remap endpoints."""
    import sys
    from types import ModuleType
    from threading import Thread, Event
    from time import sleep
    import rclpy
    from rclpy.action import ActionServer, CancelResponse, GoalResponse
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor, SingleThreadedExecutor
    from smpa_ros_interfaces.action import Listen
    from smpa_ros.loop import execute_action
    parent = ModuleType('simulation_actions')
    interface = ModuleType('simulation_actions.action')
    interface.Look = Listen
    monkeypatch.setitem(sys.modules, 'simulation_actions', parent)
    monkeypatch.setitem(sys.modules, 'simulation_actions.action', interface)
    rclpy.init(args=['--ros-args', '-r', '/look_left:=/test_smpa/look_left',
                     '-r', '/look_right:=/test_smpa/look_right'])
    server = rclpy.create_node('test_look_server')
    client = rclpy.create_node('test_look_client')
    server_executor = MultiThreadedExecutor(num_threads=3)
    client_executor = SingleThreadedExecutor()
    server_executor.add_node(server)
    client_executor.add_node(client)
    finished = Event()
    accepted = []
    def execute(handle):
        accepted.append(handle.request)
        finished.clear()
        if mode == 'timeout':
            while not handle.is_cancel_requested:
                sleep(0.005)
            handle.canceled()
        elif mode == 'aborted':
            handle.abort()
        else:
            sleep(0.02)
            handle.succeed()
        finished.set()
        return Listen.Result(success=mode == 'success')
    servers = []
    if mode != 'unavailable':
        for endpoint in ('/look_left', '/look_right'):
            servers.append(ActionServer(server, Listen, endpoint, execute,
                goal_callback=lambda request: GoalResponse.REJECT if mode == 'rejected' else GoalResponse.ACCEPT,
                cancel_callback=lambda handle: CancelResponse.ACCEPT, callback_group=ReentrantCallbackGroup()))
    thread = Thread(target=server_executor.spin, daemon=True)
    thread.start()
    def sense(directory, cycle):
        if cycle > 1:
            assert finished.is_set(), 'Next Sense began before previous action completed'
        return f'seen {cycle}'
    choices = iter(['look_left', 'look_right', 'look_left'])
    def plan(prompt, **settings):
        return json.dumps({'action': next(choices)})
    def act(action, record):
        execute_action(client, client_executor, action, record, 0.3 if mode in ('timeout', 'unavailable') else 3.0)
    try:
        if mode == 'success':
            directory = run(3, root=tmp_path, sense=sense, model=model_snapshot, plan=plan, act=act)
            assert len(accepted) == 3
            assert yaml.safe_load((directory / 'metadata.yaml').read_text())['completed_cycles'] == 3
            assert json.loads((directory / 'state/cycle_000002/act.json').read_text())['requested_action'] == 'look_right'
        else:
            with pytest.raises((RuntimeError, TimeoutError)):
                run(3, root=tmp_path, sense=sense, model=model_snapshot, plan=plan, act=act)
            directory, = (tmp_path / 'runs').iterdir()
            metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
            assert metadata['completed_cycles'] == 0 and metadata['status'] == 'failed'
            record = json.loads((directory / 'state/cycle_000001/act.json').read_text())
            assert record['success'] is False and record['error']
            assert (directory / 'world_model/iteration_001.md').is_file()
            if mode == 'rejected':
                assert record['goal_accepted'] is False
            if mode == 'aborted':
                assert record['result_status'] == 6
            if mode == 'timeout':
                assert record['cancel_requested'] is True and record['cancellation_result_status'] == 5
    finally:
        client_executor.shutdown()
        server_executor.shutdown()
        thread.join(timeout=5)
        for action_server in servers:
            action_server.destroy()
        server.destroy_node()
        client.destroy_node()
        rclpy.shutdown()
