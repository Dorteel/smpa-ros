from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import importlib.util
import json
from unittest.mock import MagicMock

import pytest
import yaml

from memory import MemoryManager
from runner import Runner
from smpa_ros.smpa import GOAL, run as experiment



def planned_action(prompt, **settings):
    return '{"action": "look_left"}'


def completed_action(action, record):
    record.update(goal_accepted=True, result_status=4, success=True)


def run(max_cycles=1, **kwargs):
    kwargs.setdefault('plan', planned_action)
    kwargs.setdefault('act', completed_action)
    goal = kwargs.pop('goal', GOAL)
    with Runner(max_cycles=max_cycles, **kwargs) as runner:
        experiment(runner, runner.limit, goal=goal)
        return runner.memory.directory


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
            assert MemoryManager.assigned_run(directory, metadata['run_id']) == (directory, metadata['run_id'])
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
    assert len(list((directory / 'state').glob('cycle_*/*.json'))) == names.index(stage) + 1


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
        MemoryManager.assigned_run('')
    with pytest.raises(ValueError, match='Invalid'):
        MemoryManager.assigned_run(tmp_path)
    memory = MemoryManager(tmp_path, goal=GOAL)
    directory, metadata = memory.directory, memory.metadata
    with pytest.raises(ValueError, match='does not match'):
        MemoryManager.assigned_run(directory, 'wrong')
    memory.finalize('completed')
    memory.finalize('failed')
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
        script = ('from runner import Runner; from smpa_ros.smpa import run\n'
                  f'with Runner(max_cycles=3, root={root!r}, sense=lambda directory, cycle: "observed", model=lambda prompt, **kw: "# World Model", '
                  f'plan=lambda prompt, **kw: ' + repr('{"action":"look_left"}') + ', '
                  'act=lambda action, record: record.update(goal_accepted=True, result_status=4, success=True)) as runner:\n'
                  '    run(runner, 3)')
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
    assert [item['executable'] for item in assignments] == ['smpa']
    args = assignments[0]['arguments']
    assert args[args.index('--max-cycles') + 1] == '3'
    assert args[args.index('--service') + 1] == ('speech' if entrypoint == 'speech' else 'vision')
    directory, = (tmp_path / 'runs').iterdir()
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert metadata['status'] == 'completed' and metadata['completed_cycles'] == 3


def test_participant_restarts_share_run(assigned_test_run):
    from unittest.mock import MagicMock
    import rclpy
    from smpa_ros.sense import SenseService
    from speech.actions import SpeechServer

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
        config='config.yaml', model_root='/models', audio_lock='/tmp/test.lock',
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
    from smpa_ros.plan import ACTION_SCHEMA
    assert planned.generate.call_args.kwargs['schema'] == ACTION_SCHEMA


@pytest.mark.parametrize('mode, actions', [('success', ('look_left', 'look_right')),
    ('success', ('look_ahead', 'turn_left')), ('success', ('turn_right', 'look_ahead')),
    *[(mode, ('look_left', 'look_right')) for mode in ('rejected', 'aborted', 'timeout', 'unavailable')]])
def test_ros_action_dispatch_and_cycle_order(tmp_path, monkeypatch, mode, actions):
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
    from smpa_ros.act import execute_action
    parent = ModuleType('simulation_actions')
    interface = ModuleType('simulation_actions.action')
    interface.Look = interface.LookAhead = interface.Turn = Listen
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
    pipeline = {'enabled_actions': list(actions)}
    servers = []
    if mode != 'unavailable':
        for endpoint in ('/' + name for name in actions):
            servers.append(ActionServer(server, Listen, endpoint, execute,
                goal_callback=lambda request: GoalResponse.REJECT if mode == 'rejected' else GoalResponse.ACCEPT,
                cancel_callback=lambda handle: CancelResponse.ACCEPT, callback_group=ReentrantCallbackGroup()))
    thread = Thread(target=server_executor.spin, daemon=True)
    thread.start()
    def sense(directory, cycle):
        if cycle > 1:
            assert finished.is_set(), 'Next Sense began before previous action completed'
        return f'seen {cycle}'
    choices = iter([actions[0], actions[1], actions[0]])
    def plan(prompt, **settings):
        return json.dumps({'action': next(choices)})
    def act(action, record):
        execute_action(client, client_executor, action, record, 0.3 if mode in ('timeout', 'unavailable') else 3.0, configuration=pipeline)
    try:
        if mode == 'success':
            directory = run(3, root=tmp_path, sense=sense, model=model_snapshot, plan=plan, act=act, configuration={'pipeline':pipeline})
            assert len(accepted) == 3
            assert yaml.safe_load((directory / 'metadata.yaml').read_text())['completed_cycles'] == 3
            assert json.loads((directory / 'state/cycle_000002/act.json').read_text())['requested_action'] == actions[1]
        else:
            with pytest.raises((RuntimeError, TimeoutError)):
                run(3, root=tmp_path, sense=sense, model=model_snapshot, plan=plan, act=act, configuration={'pipeline':pipeline})
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


@pytest.mark.parametrize('pipeline, expected', [
    ({}, (512, 128)),
    ({'modeling': {'max_output_tokens': 97}, 'planning': {'max_output_tokens': 43}}, (97, 43)),
])
def test_stage_token_budgets(tmp_path, pipeline, expected):
    model = MagicMock(side_effect=model_snapshot)
    plan = MagicMock(side_effect=planned_action)
    # Use plain callables so the loop does not use MagicMock's synthetic generate attribute.
    run(root=tmp_path, sense=lambda directory, cycle: 'observation',
        model=lambda *args, **kwargs: model(*args, **kwargs),
        plan=lambda *args, **kwargs: plan(*args, **kwargs),
        configuration={'pipeline': pipeline})
    assert model.call_args.kwargs['max_new_tokens'] == expected[0]
    assert plan.call_args.kwargs['max_new_tokens'] == expected[1]
    assert 'schema' in plan.call_args.kwargs


@pytest.mark.parametrize('stage', ['modeling', 'planning'])
@pytest.mark.parametrize('value', [0, -1, True, 1.5, '512', None])
def test_invalid_stage_token_budget(tmp_path, stage, value):
    with pytest.raises(ValueError, match='positive integer'):
        run(root=tmp_path, sense=MagicMock(),
            configuration={'pipeline': {stage: {'max_output_tokens': value}}})
    assert not (tmp_path / 'runs').exists()


def test_independent_stage_reasoning_effort(tmp_path):
    received = []
    def model(prompt, **settings):
        received.append(settings)
        return model_snapshot(prompt)
    def plan(prompt, **settings):
        received.append(settings)
        return planned_action(prompt)
    run(root=tmp_path, sense=lambda directory, cycle: 'observation', model=model, plan=plan,
        configuration={'pipeline': {'modeling': {'reasoning_effort': 'low'},
                                    'planning': {'reasoning_effort': 'high'}}})
    assert received[0] == {'max_new_tokens': 512, 'reasoning_effort': 'low', 'temperature': 0, 'response_format': 'text'}
    assert received[1]['reasoning_effort'] == 'high'
    assert received[1]['max_new_tokens'] == 128
    assert 'schema' in received[1]


@pytest.mark.parametrize('failure', [None, 'setup', 'sense', 'teardown'])
def test_runner_cleanup_finalizes_run(tmp_path, failure):
    calls = []

    def setup(directory, identity):
        calls.append('setup')
        assert MemoryManager.assigned_run(directory, identity) == (directory, identity)
        if failure == 'setup':
            raise RuntimeError('setup failed')

    def sense(directory, cycle):
        if failure == 'sense':
            raise RuntimeError('sense failed')
        return 'observation'

    def teardown():
        calls.append('teardown')
        if failure == 'teardown':
            raise RuntimeError('teardown failed')

    if failure is None:
        run(root=tmp_path, sense=sense, model=model_snapshot, setup=setup, teardown=teardown)
    else:
        with pytest.raises(RuntimeError, match=f'{failure} failed'):
            run(root=tmp_path, sense=sense, model=model_snapshot, setup=setup, teardown=teardown)
    directory, = (tmp_path / 'runs').iterdir()
    metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
    assert calls == ['setup', 'teardown']
    assert metadata['status'] == ('completed' if failure is None else 'failed')
    assert metadata['completed_at'] is not None
    assert metadata['completed_cycles'] == (0 if failure in ('setup', 'sense') else 1)


def test_experiment_repeats_skips_and_independent_runs(tmp_path):
    calls = []
    def model(prompt, **settings):
        calls.append(prompt)
        return f'world {len(calls)}'
    cleanup = MagicMock()
    with Runner(root=tmp_path, sense=lambda directory, cycle: 'observed',
                model=model, plan=planned_action, act=completed_action,
                teardown=cleanup) as runner:
        directories = []
        for _ in range(2):
            directories.append(runner.start(goal=GOAL))
            with runner.cycle():
                observation = runner.sense(prompt='describe_scene')
                first = runner.model(observation)
                second = runner.model(observation)
                action = runner.plan(first)
                runner.act(action)
                runner.act(runner.plan(second))
                runner.sense(prompt='default')
            with runner.cycle():
                # Skip Sense/Model and reuse a previous world model.
                runner.act(runner.plan(second))
            with runner.cycle():
                runner.model('explicit observation')
            runner.finish()
    assert len(set(directories)) == 2 and cleanup.call_count == 2
    for directory in directories:
        records = [json.loads(path.read_text()) for path in directory.glob('state/*/*.json')]
        records.sort(key=lambda record: record['sequence'])
        assert [record['sequence'] for record in records] == list(range(1, 12))
        assert [record['stage'] for record in records] == [
            'sense', 'model', 'model', 'plan', 'act', 'plan', 'act', 'sense', 'plan', 'act', 'model']
        state = directory / 'state/cycle_000001'
        assert {p.name for p in state.iterdir()} == {
            'sense.json', 'sense_002.json', 'model.json', 'model_002.json',
            'plan.json', 'plan_002.json', 'act.json', 'act_002.json'}
        assert {p.name for p in (directory / 'world_model').iterdir()} == {
            'iteration_001.md', 'iteration_001_002.md', 'iteration_003.md'}
        assert records[2]['previous_iteration'] == 'world_model/iteration_001.md'
        assert records[3]['source_record'] == 'state/cycle_000001/model.json'
        assert records[5]['source_record'] == 'state/cycle_000001/model_002.json'
        assert records[6]['source_record'] == 'state/cycle_000001/plan_002.json'
        assert records[-1]['previous_iteration'] == 'world_model/iteration_001_002.md'
        assert records[-1]['source_record'] is None
        assert yaml.safe_load((directory / 'metadata.yaml').read_text())['completed_cycles'] == 3
        assert (directory / 'goal.md').read_text() == GOAL


@pytest.mark.parametrize('stage', ['sense', 'model', 'plan', 'act'])
def test_explicit_prompt_selection(tmp_path, monkeypatch, stage):
    import importlib
    module = importlib.import_module('smpa_ros.' + stage)
    resources = tmp_path / 'resources'
    (resources / 'prompts' / stage).mkdir(parents=True)
    (resources / 'prompts' / stage / 'experiment.txt').write_text('Selected experiment: {input}')
    monkeypatch.setattr(module, 'files', lambda package: resources)
    with Runner(root=tmp_path, sense=lambda directory, cycle: 'observed',
                model=model_snapshot, plan=planned_action, act=completed_action) as runner:
        runner.start(goal=GOAL)
        with runner.cycle():
            if stage == 'sense':
                runner.sense(prompt='experiment')
            else:
                getattr(runner, stage)('look_left' if stage == 'act' else 'input', prompt='experiment')
    record = json.loads((runner.memory.directory / f'state/cycle_000001/{stage}.json').read_text())
    assert record['prompt'].startswith('Selected experiment:')
    assert record['prompt_name'] == 'experiment'


@pytest.mark.parametrize('stage', ['sense', 'model', 'plan', 'act'])
def test_missing_prompt_cleans_up(tmp_path, stage):
    cleanup = MagicMock()
    with pytest.raises(FileNotFoundError, match='missing_prompt.txt'):
        with Runner(root=tmp_path, sense=lambda directory, cycle: 'observed',
                    model=model_snapshot, plan=planned_action, act=completed_action,
                    teardown=cleanup) as runner:
            runner.start(goal=GOAL)
            with runner.cycle():
                args = () if stage == 'sense' else ('look_left' if stage == 'act' else 'input',)
                getattr(runner, stage)(*args, prompt='missing_prompt')
    cleanup.assert_called_once()
    assert runner.memory.metadata['status'] == 'failed'
    assert runner.memory.metadata['completed_cycles'] == 0


def test_error_between_stages_cleans_up(tmp_path):
    cleanup = MagicMock()
    with pytest.raises(KeyboardInterrupt):
        with Runner(root=tmp_path, sense=lambda directory, cycle: 'observed', teardown=cleanup) as runner:
            runner.start(goal=GOAL)
            with runner.cycle():
                runner.sense()
                raise KeyboardInterrupt
    cleanup.assert_called_once()
    assert runner.memory.metadata['status'] == 'interrupted'


def test_independent_canonical_generation_snapshot_and_prompts(tmp_path):
    from copy import deepcopy
    from smpa_ros.backends.base import GENERATION_KEYS
    from smpa_ros.plan import ACTION_SCHEMA
    from importlib.resources import files
    received = []
    pipeline = dict(stage_settings={
        'sense':dict(temperature=.2,max_output_tokens=31,reasoning_effort=None,seed=1,response_format='text'),
        'model':dict(temperature=.4,max_output_tokens=71,reasoning_effort=None,seed=2,response_format='json_schema',output_schema={
            'type':'object','properties':{'objects':{'type':'array'}},'required':['objects']}),
        'plan':dict(temperature=.6,max_output_tokens=91,reasoning_effort=None,seed=3,response_format='json_schema',output_schema=ACTION_SCHEMA),
        'act':dict.fromkeys(GENERATION_KEYS)}, stage_prompts={
        stage:dict(name='default' if stage != 'sense' else 'describe_scene',text=files('smpa_ros').joinpath(
            f"prompts/{stage}/{'describe_scene' if stage == 'sense' else 'default'}.txt").read_text())
        for stage in ('sense','model','plan','act')})
    before = deepcopy(pipeline)
    def model(prompt, **settings):
        received.append(('model', prompt, settings))
        return '{"objects":[]}'
    def plan(prompt, **settings):
        received.append(('plan', prompt, settings))
        return '{"action":"look_left"}'
    directory = run(root=tmp_path, sense=lambda directory,cycle:'observation', model=model, plan=plan,
                    configuration={'pipeline':pipeline})
    assert pipeline == before
    parameters = directory / 'parameters.yaml'
    snapshot = parameters.read_bytes()
    saved = yaml.safe_load(snapshot)
    assert saved['stage_settings'] == pipeline['stage_settings']
    assert saved['stage_prompts'] == pipeline['stage_prompts']
    assert received[0][2]['temperature'] == .4 and received[0][2]['seed'] == 2
    assert received[1][2]['temperature'] == .6 and received[1][2]['seed'] == 3
    assert received[0][2]['max_new_tokens'] == 71 and received[1][2]['max_new_tokens'] == 91
    pipeline['stage_settings']['model']['temperature'] = 1.5
    assert parameters.read_bytes() == snapshot
    for stage in ('sense','model','plan','act'):
        record = json.loads((directory / f'state/cycle_000001/{stage}.json').read_text())
        assert record['generation_parameters'] == before['stage_settings'][stage]
        assert record['prompt']
    assert json.loads((directory / 'state/cycle_000001/model.json').read_text())['prompt'] == received[0][1]


@pytest.mark.parametrize('unsafe', [{'type':'object'}, dict(type='object', properties={'action':{'type':'string'}},required=['action'],additionalProperties=False)])
def test_plan_canonical_schema_cannot_bypass_ros_contract(tmp_path, unsafe):
    attempted = []
    with pytest.raises(ValueError, match='fixed ROS action contract'):
        run(root=tmp_path, sense=lambda directory,cycle:'observation', model=model_snapshot,
            plan=lambda *args,**kwargs: attempted.append('plan'),
            act=lambda *args: attempted.append('act'),
            configuration={'pipeline':{'stage_settings':{'plan':{
                'max_output_tokens':32,'response_format':'json_schema','output_schema':unsafe}}}})
    assert not attempted


def test_unregistered_local_paths_survive_runner_snapshot(tmp_path,monkeypatch):
    from test_cognition import local_model_directory
    from smpa_ros.model import resolve_vision_model
    text=local_model_directory(tmp_path/'models/llm/new_text')
    vision=local_model_directory(tmp_path/'models/vlm/new_vision',vision=True)
    config={'vision':{'backend':'transformers','path':str(vision),'model_id':'custom/vision'},
            'modeling':{'backend':'transformers','path':str(text),'model_id':'custom/text'}}
    source=tmp_path/'config.yaml'
    source.write_text(yaml.safe_dump(config))
    constructor=MagicMock()
    monkeypatch.setattr('smpa_ros.backends.transformers.TransformersBackend',constructor)
    with Runner(['smpa','--config',str(source),'--world-model-root',str(tmp_path/'memory')]) as runner:
        runner._setup=runner._teardown=None
        directory=runner.start(goal=GOAL)
        saved=yaml.safe_load((directory/'parameters.yaml').read_text())
        assert saved['model']['path']==str(vision)
        assert 'modeling' not in saved and 'planning' not in saved
        assert saved['_run']['stage_models']['sense']['path']==str(vision)
        assert saved['_run']['stage_models']['model']['path']==str(vision)
        assert saved['_run']['stage_models']['sense']['model_id']=='custom/vision'
        source.write_text('vision: {path: /missing}\n')
        assert resolve_vision_model(saved,tmp_path/'no_registry',tmp_path/'models')['path']==str(vision)
        constructor.assert_not_called()


def test_action_whitelist_filters_schema_and_rejects_execution(tmp_path):
    from smpa_ros.plan import enabled_actions
    from smpa_ros.backends.base import stage_generation
    pipeline = {'enabled_actions': ['look_right']}
    assert list(enabled_actions(pipeline)) == ['look_right']
    assert stage_generation(pipeline, 'plan')['output_schema']['properties']['action']['enum'] == ['look_right']
    executor = MagicMock()
    with pytest.raises(ValueError, match='enabled whitelist'):
        run(root=tmp_path, sense=lambda *args: 'scene', model=model_snapshot,
            plan=planned_action, act=executor, configuration={'pipeline': pipeline})
    executor.assert_not_called()
    with Runner(root=tmp_path / 'direct', sense=lambda *args: 'scene', act=executor,
                configuration={'pipeline': pipeline}) as runner:
        runner.start(goal=GOAL)
        with runner.cycle():
            with pytest.raises(ValueError, match='enabled whitelist'):
                runner.act('look_left')
    executor.assert_not_called()


@pytest.mark.parametrize('limit, expected_status, expected_cycles', [(10, 'completed', 4), (2, 'cycle_limit', 2), (1, 'cycle_limit', 1)])
def test_finite_question_refinement_and_consolidation(tmp_path, monkeypatch, limit, expected_status, expected_cycles):
    from types import SimpleNamespace
    from PIL import Image
    from smpa_ros.sense import SenseService
    from smpa_ros.plan import refinement_settings
    pipeline = {'enabled_actions': ['observe'], 'scene_refinement': {
        'enabled': True, 'properties': ['color', 'material', 'texture']}}
    backend = MagicMock()
    def generate(prompt, **options):
        properties = options.get('schema', {}).get('properties', {})
        if 'entities' in properties:
            return '{"entities":[{"id":"cup_1","description":"cup on table"}]}'
        if 'action' in properties:
            return json.dumps(dict(action='observe', target=properties['target']['enum'][0],
                                   question=properties['question']['enum'][0]))
        return '# World Model\n' + prompt
    backend.generate.side_effect = generate
    backend.describe.side_effect = [json.dumps(dict(status=status, answer=answer)) for status,answer in
        [('answered','red'), ('visually_indeterminable','Cannot infer material'), ('unanswered','No conclusion')]]
    view = SimpleNamespace(backend=backend, image=Image.new('RGB',(2,2),'red'),
        image_sha256='frozen-hash', pipeline=pipeline, refinement=refinement_settings(pipeline), default_prompt='Describe the scene.')
    view.focus = lambda target,question,**options: SenseService.focus(view,target,question,**options)
    constructors = MagicMock(side_effect=AssertionError('No new inference instance is permitted'))
    monkeypatch.setattr('smpa_ros.backends.transformers.TransformersVLM', constructors)
    with Runner(root=tmp_path, max_cycles=limit, sense=lambda *args:'A cup on a table.',
                model=backend, plan=backend, configuration={'pipeline':pipeline}) as runner:
        runner.backend = backend
        runner.sense_node = view
        runner._act = runner.dispatch
        assert runner._model is runner._plan is view.backend
        experiment(runner, runner.limit)
        directory = runner.memory.directory
        state = json.loads((directory/'state/refinement.json').read_text())
        assert state['status'] == expected_status
        assert runner.memory.metadata['completed_cycles'] == expected_cycles
        assert state['image_sha256']=='frozen-hash'
        attempted = [entry for entry in state['questions'] if entry['attempted']]
        assert len(attempted)==min(limit-1,3)
        assert len({entry['question'] for entry in attempted})==len(attempted)
        assert [entry['status'] for entry in attempted] == ['answered','visually_indeterminable','unanswered'][:len(attempted)]
        assert backend.describe.call_count == len(attempted)
        final_model=json.loads((directory/f'state/cycle_{expected_cycles:06d}/model.json').read_text())
        assert all(entry['answer'] in final_model['prompt'] for entry in attempted)
        assert json.loads((directory/f'state/cycle_{expected_cycles:06d}/act.json').read_text())['requested_action'] is None
        for call in backend.describe.call_args_list:
            assert call.args[0].getpixel((0,0)) == (255,0,0)
            assert 'cup_1' in call.args[1]
            assert call.kwargs['response_format']=='json_schema'
        if expected_status=='completed':
            last_model=json.loads((directory/'state/cycle_000004/model.json').read_text())
            assert 'No conclusion' in last_model['prompt']
            assert json.loads((directory/'state/cycle_000004/act.json').read_text())['requested_action'] is None
            assert runner.experiment_complete is True
        if attempted:
            runner.sense_node = view  # Run cleanup releases it; inspect duplicate protection with the same supplied view.
            with pytest.raises(ValueError, match='unattempted'):
                runner.focused_observation({'parameters': {key: attempted[0][key] for key in ('target','question')}})
        parameters=yaml.safe_load((directory/'parameters.yaml').read_text())
        assert parameters['_run']['enabled_actions']==['observe']
        assert parameters['scene_refinement']==refinement_settings(pipeline)
        assert len(list((directory/'observations/vision').glob('observe_*.json')))==len(attempted)
    constructors.assert_not_called()


@pytest.mark.parametrize('decision', [
    {'action':'observe'}, {'action':'observe','question':''},
    {'action':'observe','question':'Color?','target':42},
    {'action':'look_left','question':'Color?'}, {'action':'look_left','extra':True}])
def test_parameterized_action_validation_remains_strict(decision):
    from smpa_ros.plan import validate_decision, AVAILABLE_ACTIONS
    with pytest.raises(ValueError):
        validate_decision(decision, AVAILABLE_ACTIONS)
