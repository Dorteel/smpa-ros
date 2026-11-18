"""Exercise the experiment's explicit action order with mocked stage clients."""
import json
from types import SimpleNamespace

import pytest
from memory import MemoryManager
from smpa_ros_interfaces import action
from smpa_ros.smpa import run_actions


@pytest.fixture
def stage_types(monkeypatch):
    """Supply ungenerated goal classes without starting cognitive ROS nodes.

    The goal constructor keeps request fields unchanged so tests can verify
    run identity, provenance and previous world-model propagation.
    """
    for name in ('Sense', 'Model', 'Plan', 'Act'):
        monkeypatch.setattr(action, name, SimpleNamespace(Goal=SimpleNamespace), raising=False)


def test_explicit_sequence_and_provenance(stage_types, tmp_path):
    """Run two complete cycles and check every action's inputs and ordering.

    Mock stage outcomes and confirm each persisted record is handed to the next
    request, the second cycle receives the first world-model snapshot, and both
    cycles finish only after their Act result succeeds.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    calls, events = [], []
    def invoke(stage, action_type, goal):
        """Return distinct deterministic stage outputs without ROS execution."""
        calls.append((stage, goal))
        number = len([item for item in calls if item[0] == stage])
        if stage == 'sense':
            return SimpleNamespace(observation=f'observation-{number}', record_path=f'sense-{number}')
        if stage == 'model':
            return SimpleNamespace(world_model=f'world-{number}', record_path=f'model-{number}', snapshot_path=f'snapshot-{number}')
        if stage == 'plan':
            return SimpleNamespace(action_json='{"action":"look_left"}', record_path=f'plan-{number}')
        return SimpleNamespace(success=True)
    def notify(stage, event):
        """Retain lifecycle events in their actual publication order."""
        events.append((stage, event))
    run_actions(memory, 2, invoke, notify=notify)
    assert [name for name, _ in calls] == ['sense', 'model', 'plan', 'act'] * 2
    assert calls[1][1].source_record == 'sense-1' and calls[1][1].previous_snapshot == ''
    assert calls[2][1].source_record == 'model-1' and calls[3][1].source_record == 'plan-1'
    assert calls[5][1].previous_snapshot == 'snapshot-1'
    assert all(goal.run_id == memory.metadata['run_id'] for _, goal in calls)
    assert memory.metadata['completed_cycles'] == 2 and len(events) == 16


def test_failed_action_keeps_cycle_incomplete(stage_types, tmp_path):
    """Leave the active cycle incomplete when a stage returns a failure.

    Fail the Act result after the three earlier mocked stages. The experiment
    reports the actual failed stage and never advances its completed-cycle count.
    """
    memory = MemoryManager(tmp_path, goal='Find cup')
    events = []
    def invoke(stage, action_type, goal):
        """Return minimal successful data until Act reports a failure."""
        return {'sense': SimpleNamespace(observation='seen', record_path='sense'),
                'model': SimpleNamespace(world_model='world', record_path='model', snapshot_path='snapshot'),
                'plan': SimpleNamespace(action_json=json.dumps({'action': 'look_left'}), record_path='plan'),
                'act': SimpleNamespace(success=False)}[stage]
    def notify(stage, event):
        """Capture the failed stage notification for the lifecycle observer."""
        events.append((stage, event))
    with pytest.raises(RuntimeError, match='Act did not complete'):
        run_actions(memory, 1, invoke, notify=notify)
    assert memory.metadata['completed_cycles'] == 0 and not memory.in_cycle
    assert events[-1] == ('act', 'stage_failed')


def test_sequence_runs_real_stage_callbacks_with_mocked_dependencies(monkeypatch, tmp_path):
    """Execute the full script sequence against real stage callback functions.

    Fake only the ungenerated action messages, inference, sensors and robot
    operation. The actual stage implementations and run memory write their
    normal records. Confirm source links and cycle completion across all stages.
    """
    import importlib
    import sys
    from unittest.mock import Mock
    from smpa_ros_interfaces import action

    fields = {'Sense': ('observation', 'record_path'), 'Model': ('world_model', 'record_path', 'snapshot_path'),
              'Plan': ('action_json', 'record_path'), 'Act': ('success', 'record_path')}
    for name, names in fields.items():
        def result_factory(names=names):
            """Create empty result fields matching one ungenerated action."""
            return SimpleNamespace(**{key: False if key == 'success' else '' for key in names})
        monkeypatch.setattr(action, name, SimpleNamespace(Goal=SimpleNamespace, Result=result_factory), raising=False)
    for name in ('DescribeStructuredImage', 'GenerateStructuredChat'):
        if not hasattr(action, name):
            monkeypatch.setattr(action, name, SimpleNamespace(Goal=SimpleNamespace), raising=False)
    module_names = ['smpa_ros.sense_node', 'smpa_ros.model_node', 'smpa_ros.plan_node', 'smpa_ros.act_node']
    for name in module_names:
        sys.modules.pop(name, None)
    sense, model, plan, act = [importlib.import_module(name) for name in module_names]
    pipeline = {'enabled_actions': ['look_left'], 'sensing': {'vision': False},
        'stage_prompts': {'sense': {'name': 'chosen', 'text': 'Sense.'},
                          'model': {'name': 'chosen', 'text': '{input} {previous_world_model}'},
                          'plan': {'name': 'chosen', 'text': '{goal} {input} {available_actions}'},
                          'act': {'name': 'chosen', 'text': '{input}'}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    def observe_once(*args, **kwargs):
        """Provide a fixed sensory observation without touching hardware."""
        return '{"vision":"cup"}', ''
    monkeypatch.setattr(sense, 'observe', observe_once)
    def world(prompt, **settings):
        """Return a fixed world model without loading a text model."""
        return 'world with cup'
    def decision(prompt, **settings):
        """Return a valid fixed decision without loading a planner."""
        return '{"action":"look_left"}'
    def dispatch(name, record, **context):
        """Report a successful robot operation without starting simulation."""
        record.update(goal_accepted=True, result_status=4, success=True)
    def parameter(name):
        """Provide each stage its memory root and bounded timeout."""
        return SimpleNamespace(value=1.0 if name == 'timeout' else str(tmp_path))
    nodes = {'sense': SimpleNamespace(get_parameter=parameter, wait=Mock(), describe=Mock(), get_logger=lambda: Mock()),
             'model': SimpleNamespace(get_parameter=lambda name: SimpleNamespace(value='model') if name == 'default_capability' else parameter(name), generate=world, get_logger=lambda: Mock()),
             'plan': SimpleNamespace(get_parameter=parameter, generate=decision, get_logger=lambda: Mock()),
             'act': SimpleNamespace(get_parameter=parameter, dispatch=dispatch, get_logger=lambda: Mock())}
    callbacks = {'sense': sense.SenseNode.execute, 'model': model.ModelNode.execute,
                 'plan': plan.PlanNode.execute, 'act': act.ActNode.execute}
    def invoke(stage, action_type, goal):
        """Deliver one action goal to the real stage callback and check success."""
        handle = SimpleNamespace(request=goal, succeed=Mock(), abort=Mock())
        result = callbacks[stage](nodes[stage], handle)
        handle.succeed.assert_called_once()
        handle.abort.assert_not_called()
        return result
    try:
        run_actions(memory, 1, invoke)
        directory = memory.directory / 'state/cycle_000001'
        records = {name: json.loads((directory / f'{name}.json').read_text())
                   for name in ('sense', 'model', 'plan', 'act')}
        assert records['model']['source_record'] == 'state/cycle_000001/sense.json'
        assert records['plan']['source_record'] == 'state/cycle_000001/model.json'
        assert records['act']['source_record'] == 'state/cycle_000001/plan.json'
        assert records['act']['status'] == 'succeeded' and memory.metadata['completed_cycles'] == 1
    finally:
        for name in module_names:
            sys.modules.pop(name, None)


def test_experiment_acquires_snapshots_runs_and_releases(stage_types, tmp_path):
    """Run the lifecycle with mocked ROS clients and a real persistent run.

    Capture the server snapshot before any stage, execute the script's sequence,
    then release the same owner and finalize metadata. The mock transport never
    opens sensors, a model, a GPU or a simulator.
    """
    from smpa_ros.smpa import execute_experiment
    memory = MemoryManager(tmp_path, goal='Find cup')
    identity = memory.metadata['run_id']
    snapshot = dict(run_id=identity, server_instance_id='server-a', active_model='fixture',
                    loading={'model_id': 'fixture'}, generation={'temperature': 0.0})
    changes, stages, events = [], [], []
    def set_parameters(values):
        """Record acquisition or owner-checked release and accept it."""
        changes.append(values[0].value)
        return SimpleNamespace(result=SimpleNamespace(successful=True, reason=''))
    parameters = SimpleNamespace(wait_for_services=lambda timeout_sec: True, set_parameters_atomically=set_parameters,
        get_parameters=lambda names: SimpleNamespace(values=[SimpleNamespace(string_value=json.dumps(snapshot))]))
    responses = {'sense': SimpleNamespace(observation='seen', record_path='sense'),
                 'model': SimpleNamespace(world_model='world', record_path='model', snapshot_path='snapshot'),
                 'plan': SimpleNamespace(action_json='{"action":"look_left"}', record_path='plan'),
                 'act': SimpleNamespace(success=True)}
    clients = {}
    for name in responses:
        def send(goal, name=name):
            """Return a successful mocked result for the selected stage."""
            stages.append(name)
            return SimpleNamespace(accepted=True, get_result_async=lambda: SimpleNamespace(status=4, result=responses[name]))
        clients[name] = SimpleNamespace(wait_for_server=lambda timeout_sec: True, send_goal_async=send)
    def wait(future, timeout, operation):
        """Return the already completed mock response."""
        return future
    def notify(stage, event):
        """Record lifecycle events in their emitted order."""
        events.append((stage, event))
    execute_experiment(memory, 1, clients, parameters, wait, 1.0, notify=notify)
    assert changes == [identity, 'release:' + identity]
    assert stages == ['sense', 'model', 'plan', 'act']
    assert memory.metadata['inference'] == snapshot and memory.metadata['status'] == 'completed'
    assert events[0] == ('', 'run_started') and events[-1] == ('', 'run_completed')


def test_experiment_keeps_freeze_when_stage_termination_unknown(stage_types, tmp_path):
    """Leave inference owned if a timed-out stage cannot be confirmed finished.

    Simulate an accepted Sense goal whose result never arrives. Cleanup must not
    release the freeze, must finalize the run as failed, and must report that
    the remote operation is still unconfirmed.
    """
    from smpa_ros.smpa import execute_experiment
    memory = MemoryManager(tmp_path, goal='Find cup')
    identity = memory.metadata['run_id']
    snapshot = dict(run_id=identity, server_instance_id='server-a', active_model='fixture', loading={}, generation={})
    changes = []
    def set_parameters(values):
        """Record and accept any attempted ownership transition."""
        changes.append(values[0].value)
        return SimpleNamespace(result=SimpleNamespace(successful=True, reason=''))
    parameters = SimpleNamespace(wait_for_services=lambda timeout_sec: True, set_parameters_atomically=set_parameters,
        get_parameters=lambda names: SimpleNamespace(values=[SimpleNamespace(string_value=json.dumps(snapshot))]))
    client = SimpleNamespace(wait_for_server=lambda timeout_sec: True,
        send_goal_async=lambda goal: SimpleNamespace(accepted=True, get_result_async=lambda: 'pending'))
    def wait(future, timeout, operation):
        """Timeout every attempt to observe the pending stage result."""
        if future == 'pending':
            raise TimeoutError('stage still active')
        return future
    with pytest.raises(RuntimeError, match='cleanup was not confirmed'):
        execute_experiment(memory, 1, {'sense': client}, parameters, wait, 1.0)
    assert changes == [identity] and memory.metadata['status'] == 'failed'


def test_ros_entry_point_uses_independent_action_clients(stage_types, monkeypatch, tmp_path):
    """Exercise the command entry path using mocked ROS clients and inference.

    The real experiment lifecycle must acquire and persist its snapshot, send
    all four stage goals in script order, release ownership and destroy clients.
    No stage server, speech process, model or simulator is started.
    """
    import rclpy
    import rclpy.action
    import rclpy.node
    import rclpy.parameter_client
    from unittest.mock import Mock
    import smpa_ros.smpa as script
    memory = MemoryManager(tmp_path, goal='Find cup')
    identity = memory.metadata['run_id']
    profile = SimpleNamespace(limit=1, modalities={'hear': False}, options=SimpleNamespace(timeout=1.0))
    monkeypatch.setattr(script, 'prepare_experiment', lambda args, goal: (profile, memory))
    monkeypatch.setattr(rclpy, 'init', lambda **kwargs: None)
    monkeypatch.setattr(rclpy, 'ok', lambda: True)
    monkeypatch.setattr(rclpy, 'shutdown', lambda: None)
    node = SimpleNamespace(destroy_node=Mock())
    monkeypatch.setattr(rclpy.node, 'Node', lambda name: node)
    stages, changes, clients = [], [], []
    responses = {'sense': SimpleNamespace(observation='seen', record_path='sense'),
                 'model': SimpleNamespace(world_model='world', record_path='model', snapshot_path='snapshot'),
                 'plan': SimpleNamespace(action_json='{"action":"look_left"}', record_path='plan'),
                 'act': SimpleNamespace(success=True)}
    def action_client(node, action_type, endpoint):
        """Create a fake client for the requested cognitive action endpoint."""
        stage = endpoint.rsplit('/', 1)[-1]
        def send(goal):
            """Return the fixed response after recording the action order."""
            stages.append(stage)
            return SimpleNamespace(accepted=True, get_result_async=lambda: SimpleNamespace(status=4, result=responses[stage]))
        client = SimpleNamespace(wait_for_server=lambda timeout_sec: True, send_goal_async=send, destroy=Mock())
        clients.append(client)
        return client
    monkeypatch.setattr(rclpy.action, 'ActionClient', action_client)
    snapshot = dict(run_id=identity, server_instance_id='server-a', active_model='fixture', loading={}, generation={})
    def set_parameters(values):
        """Record the actual freeze acquisition and owner-checked release."""
        changes.append(values[0].value)
        return SimpleNamespace(result=SimpleNamespace(successful=True, reason=''))
    parameters = SimpleNamespace(wait_for_services=lambda timeout_sec: True, set_parameters_atomically=set_parameters,
        get_parameters=lambda names: SimpleNamespace(values=[SimpleNamespace(string_value=json.dumps(snapshot))]))
    monkeypatch.setattr(rclpy.parameter_client, 'AsyncParameterClient', lambda node, endpoint: parameters)
    monkeypatch.setattr(script, 'wait_for_ros', lambda node, future, timeout, operation: future)
    events = []
    def notify(run_id, stage, event):
        """Retain the run identity and lifecycle outcome for assertions."""
        events.append((run_id, stage, event))
    script.run_ros_experiment(['smpa'], goal='Find cup', notify=notify)
    assert stages == ['sense', 'model', 'plan', 'act']
    assert changes == [identity, 'release:' + identity]
    assert memory.metadata['status'] == 'completed' and memory.metadata['inference'] == snapshot
    assert events[0] == (identity, '', 'run_started') and events[-1] == (identity, '', 'run_completed')
    assert len(clients) == 4 and all(client.destroy.call_count == 1 for client in clients)
    node.destroy_node.assert_called_once()


def test_prepare_experiment_preserves_configuration_without_loading_model(monkeypatch, tmp_path):
    """Create run metadata from the existing parser without constructing a backend.

    Use the repository configuration and replace only catalogue resolution with
    a fixed model identity. Verify the requested cycle limit, goal and frozen
    stage prompts are persisted for the independent stage servers.
    """
    from pathlib import Path
    from smpa_ros import smpa as script
    def resolve(profile):
        """Supply a resolved model identity without reading or loading weights."""
        profile.models = {stage: {'model_id': 'fixture/model', 'backend': 'transformers'}
                          for stage in ('vision', 'model', 'plan')}
    monkeypatch.setattr(script, 'resolve_experiment_models', resolve)
    configuration = Path(__file__).resolve().parents[1] / 'smpa_ros/config.yaml'
    profile, memory = script.prepare_experiment(['smpa', '--config', str(configuration),
        '--world-model-root', str(tmp_path), '--max-cycles', '2'], '# Requested goal')
    assert profile.limit == 2 and memory.metadata['max_cycles'] == 2
    assert memory.metadata['models']['model']['model_id'] == 'fixture/model'
    assert (memory.directory / 'goal.md').read_text() == '# Requested goal'
    import yaml
    frozen = yaml.safe_load((memory.directory / 'parameters.yaml').read_text())
    assert set(frozen['stage_prompts']) >= {'sense', 'model', 'plan', 'act'}
    assert frozen['_run']['max_cycles'] == 2


def test_transferred_configuration_matches_existing_runner(tmp_path):
    """Retain Runner's configuration, modality and command-line semantics.

    Resolve the same repository configuration through the previous parser and the
    transferred experiment function. Compare the frozen settings and selected
    limits without creating a run, loading a model or starting ROS participants.
    """
    from pathlib import Path
    from runner import Runner
    from smpa_ros.smpa import configure_experiment
    configuration = Path(__file__).resolve().parents[1] / 'smpa_ros/config.yaml'
    arguments = ['smpa', '--config', str(configuration), '--world-model-root', str(tmp_path),
                 '--max-cycles', '3', '--vision', 'false', '--hear', 'false']
    previous, transferred = SimpleNamespace(), SimpleNamespace()
    Runner.configure(previous, arguments)
    configure_experiment(transferred, arguments)
    assert transferred.config == previous.config
    assert transferred.original_config == previous.original_config
    assert transferred.modalities == previous.modalities
    assert transferred.limit == previous.limit == 3
    assert transferred.action_timeout == previous.action_timeout
    assert transferred.options == previous.options


def test_transferred_model_selection_matches_runner():
    """Preserve API model selection without creating a backend instance.

    Compare the transferred catalogue-selection function with the prior Runner
    logic for the same configured API model. Neither path opens an API client or
    performs inference, and both retain the same identities for cognitive stages.
    """
    from copy import deepcopy
    from runner import Runner
    from smpa_ros.smpa import resolve_experiment_models
    config = {'model': {'backend': 'api', 'provider': 'openai', 'model_id': 'fixture/vision'}}
    def profile():
        """Create independent mutable selection inputs for the two paths."""
        return SimpleNamespace(config=deepcopy(config), options=SimpleNamespace(model_root='/tmp/models'),
                               modalities={'hear': False})
    previous, transferred = profile(), profile()
    Runner.initialize_backends(previous)
    resolve_experiment_models(transferred)
    assert transferred.config == previous.config and transferred.selected == previous.selected
    assert transferred.models == previous.models
    assert transferred.model_identifier == previous.model_identifier == 'fixture/vision'
