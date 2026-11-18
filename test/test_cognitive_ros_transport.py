"""Exercise generated cognitive actions over ROS with mocked external operations."""
import json
from threading import Event, Thread

import pytest
import rclpy
from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter

from memory import MemoryManager
from smpa_ros.smpa import run_actions
from smpa_ros_interfaces.action import Sense, Model, Plan, Act


def wait_future(future, timeout=10):
    """Wait for a ROS future completed by the executor and return its result.

    The test runs the executor in a separate thread. Wake when the given
    response finishes and fail instead of waiting indefinitely for discovery,
    goal acceptance, or an action result.
    """
    done = Event()
    future.add_done_callback(lambda _: done.set())
    if not done.wait(timeout):
        raise TimeoutError('ROS action response did not arrive')
    return future.result()


def test_real_action_transport_and_script_sequence(tmp_path, monkeypatch):
    """Send generated actions through four real ROS action servers.

    Construct independently owned stage nodes and a separate client node. Mock
    only sensor acquisition, model output, planning output and robot execution.
    Reject unsupported capabilities, then let the actual experiment script run
    two cycles using default and explicit capabilities. Check ROS terminal
    states, source records, shared run identity and completion before draining
    and destroying every node and the ROS context.
    """
    import smpa_ros.sense_node as sense
    import smpa_ros.model_node as model
    import smpa_ros.plan_node as plan
    import smpa_ros.act_node as act
    monkeypatch.setattr(sense, 'observe', lambda *a, **k: ('{"vision":"cup"}', ''))
    monkeypatch.setattr(model.ModelNode, 'generate', lambda *a, **k: 'world with cup')
    monkeypatch.setattr(plan.PlanNode, 'generate', lambda *a, **k: '{"action":"look_left"}')
    def dispatch(self, name, record, **context):
        """Report one successful robot action without accessing hardware."""
        record.update(goal_accepted=True, result_status=4, success=True)
    monkeypatch.setattr(act.ActNode, 'dispatch', dispatch)
    pipeline = {'enabled_actions': ['look_left'], 'sensing': {'vision': False},
        'stage_prompts': {'sense': {'name': 'chosen', 'text': 'Sense.'},
            'model': {'name': 'chosen', 'text': '{input} {previous_world_model}'},
            'plan': {'name': 'chosen', 'text': '{goal} {input} {available_actions}'},
            'act': {'name': 'chosen', 'text': '{input}'}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    rclpy.init(args=[])
    nodes, clients = [], {}
    executor = MultiThreadedExecutor(num_threads=2)
    thread = None
    try:
        options = [Parameter('memory_root', value=str(tmp_path)), Parameter('timeout', value=3.0)]
        nodes = [kind(parameter_overrides=options) for kind in
                 (sense.SenseNode, model.ModelNode, plan.PlanNode, act.ActNode)]
        client_node = Node('cognitive_transport_test')
        nodes.append(client_node)
        types = {'sense': Sense, 'model': Model, 'plan': Plan, 'act': Act}
        clients = {name: ActionClient(client_node, kind, '/smpa/' + name) for name, kind in types.items()}
        for node in nodes:
            executor.add_node(node)
        thread = Thread(target=executor.spin, daemon=True)
        thread.start()
        for name, client in clients.items():
            assert client.wait_for_server(timeout_sec=10), name
            rejected = types[name].Goal(run_id=memory.metadata['run_id'], capability='unsupported')
            assert not wait_future(client.send_goal_async(rejected)).accepted
        order = []
        def invoke(stage, action_type, goal):
            """Send the script-selected goal and return its successful result."""
            order.append(stage)
            assert goal.run_id == memory.metadata['run_id']
            if len(order) > 4:
                goal.capability = stage
            handle = wait_future(clients[stage].send_goal_async(goal))
            assert handle.accepted
            response = wait_future(handle.get_result_async())
            assert response.status == GoalStatus.STATUS_SUCCEEDED, stage
            return response.result
        run_actions(memory, 2, invoke)
        assert order == ['sense', 'model', 'plan', 'act'] * 2
        assert memory.metadata['completed_cycles'] == 2
        for cycle in (1, 2):
            directory = memory.directory / f'state/cycle_{cycle:06d}'
            records = {name: json.loads((directory / f'{name}.json').read_text())
                       for name in ('sense', 'model', 'plan', 'act')}
            assert [records[name]['sequence'] for name in ('sense', 'model', 'plan', 'act')] == list(range((cycle - 1) * 4 + 1, cycle * 4 + 1))
            assert records['model']['source_record'].endswith('/sense.json')
            assert records['plan']['source_record'].endswith('/model.json')
            assert records['act']['source_record'].endswith('/plan.json')
        invalid = Sense.Goal(run_id='00000000-0000-0000-0000-000000000000', capability='sense')
        failed = wait_future(clients['sense'].send_goal_async(invalid))
        assert failed.accepted
        assert wait_future(failed.get_result_async()).status == GoalStatus.STATUS_ABORTED
    finally:
        executor.shutdown(timeout_sec=10)
        if thread is not None:
            thread.join(timeout=10)
        for client in clients.values():
            client.destroy()
        for node in reversed(nodes):
            node.destroy_node()
        rclpy.shutdown()


def stage_process(stage, root, stop):
    """Serve one stage in its own process with only external operations mocked.

    Select the named existing node, replace its external work with deterministic
    responses, and spin until the parent signals shutdown. Destroy the node and
    context so the parent can verify that each process terminates cleanly.
    """
    import smpa_ros.sense_node as sense
    import smpa_ros.model_node as model
    import smpa_ros.plan_node as plan
    import smpa_ros.act_node as act
    sense.observe = lambda *a, **k: ('{"vision":"cup"}', '')
    model.ModelNode.generate = lambda *a, **k: 'world with cup'
    plan.PlanNode.generate = lambda *a, **k: '{"action":"look_left"}'
    def dispatch(self, name, record, **context):
        """Return a successful robot result without sending a physical command."""
        record.update(goal_accepted=True, result_status=4, success=True)
    act.ActNode.dispatch = dispatch
    kinds = {'sense': sense.SenseNode, 'model': model.ModelNode,
             'plan': plan.PlanNode, 'act': act.ActNode}
    rclpy.init(args=[])
    node = kinds[stage](parameter_overrides=[Parameter('memory_root', value=root), Parameter('timeout', value=3.0)])
    try:
        while not stop.is_set():
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_node()
        rclpy.shutdown()


def test_separate_stage_processes_and_script_sequence(tmp_path):
    """Run the real script sequence over DDS to four separate stage processes.

    Each process owns exactly one stage server and mocks its external sensor,
    model or robot work. The parent sends generated goals, checks the persisted
    links and run identity, and then asks every stage to stop cleanly.
    """
    import multiprocessing
    import yaml
    from types import SimpleNamespace
    from rclpy.parameter_client import AsyncParameterClient
    from smpa_llm.server import InferenceServer
    from smpa_ros.smpa import execute_experiment
    context = multiprocessing.get_context('spawn')
    stop = context.Event()
    pipeline = {'enabled_actions': ['look_left'], 'sensing': {'vision': False},
        'stage_prompts': {'sense': {'name': 'chosen', 'text': 'Sense.'},
            'model': {'name': 'chosen', 'text': '{input} {previous_world_model}'},
            'plan': {'name': 'chosen', 'text': '{goal} {input} {available_actions}'},
            'act': {'name': 'chosen', 'text': '{input}'}}}
    memory = MemoryManager(tmp_path, goal='Find cup', configuration={'pipeline': pipeline})
    names = ('sense', 'model', 'plan', 'act')
    processes = [context.Process(target=stage_process, args=(name, str(tmp_path), stop)) for name in names]
    for process in processes:
        process.start()
    registry = tmp_path / 'models.yaml'
    registry.write_text(yaml.safe_dump({'models': {'fixture': {'backend': 'transformers', 'model_id': 'fixture', 'path': 'fake'}}}))
    rclpy.init(args=[])
    server = InferenceServer(loader=lambda *args: SimpleNamespace(generate=lambda *a, **k: 'fixture output'),
                             parameter_overrides=[Parameter('registry_file', value=str(registry))])
    server.replace_model('fixture')
    node = Node('cognitive_process_transport_test')
    types = {'sense': Sense, 'model': Model, 'plan': Plan, 'act': Act}
    clients = {name: ActionClient(node, kind, '/smpa/' + name) for name, kind in types.items()}
    parameters = AsyncParameterClient(node, '/llm')
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(server)
    executor.add_node(node)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    events = []
    try:
        for client in clients.values():
            assert client.wait_for_server(timeout_sec=15)
        def wait(future, timeout, operation):
            """Wait for the executor to complete a named ROS response."""
            return wait_future(future, timeout)
        def notify(stage, event):
            """Capture lifecycle notifications in the order sent by the script."""
            events.append((stage, event))
        execute_experiment(memory, 1, clients, parameters, wait, 10, notify=notify)
        assert [stage for stage, event in events if event == 'stage_started'] == list(names)
        assert memory.metadata['status'] == 'completed'
        assert memory.metadata['inference']['run_id'] == memory.metadata['run_id']
        assert memory.metadata['inference']['loading']['model_id'] == 'fixture'
        assert server.get_parameter('experiment_run_id').value == ''
        assert memory.metadata['completed_cycles'] == 1
        directory = memory.directory / 'state/cycle_000001'
        for previous, stage in zip(names, names[1:]):
            record = json.loads((directory / f'{stage}.json').read_text())
            assert record['source_record'].endswith('/' + previous + '.json')
    finally:
        executor.shutdown(timeout_sec=10)
        thread.join(timeout=10)
        for client in clients.values():
            client.destroy()
        node.destroy_node()
        server.destroy_node()
        rclpy.shutdown()
        stop.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        assert all(process.exitcode == 0 for process in processes)


def test_real_inference_freeze_and_standalone_rejection(tmp_path):
    """Freeze a fake loaded model through real ROS services and action goals.

    Use the actual inference server and parameter services with an injected
    weight-free backend. Acquire a run freeze, verify its saved snapshot, reject
    a standalone request and a generation parameter edit while owned, then
    release the exact owner and shut down the ROS nodes.
    """
    import yaml
    from types import SimpleNamespace
    from rclpy.parameter_client import AsyncParameterClient
    from smpa_llm.server import InferenceServer
    from smpa_ros.smpa import acquire_inference, release_inference, verify_inference
    from smpa_ros_interfaces.action import GenerateText
    registry = tmp_path / 'models.yaml'
    registry.write_text(yaml.safe_dump({'models': {'fixture': {'backend': 'transformers', 'model_id': 'fixture', 'path': 'fake'}}}))
    memory = MemoryManager(tmp_path, goal='Find cup')
    rclpy.init(args=[])
    server = InferenceServer(loader=lambda *args: SimpleNamespace(generate=lambda *a, **k: 'fixture output'),
                             parameter_overrides=[Parameter('registry_file', value=str(registry))])
    server.replace_model('fixture')
    node = Node('inference_freeze_transport_test')
    client = ActionClient(node, GenerateText, '/llm/generate')
    parameters = AsyncParameterClient(node, '/llm')
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(server)
    executor.add_node(node)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    def wait(future, timeout, operation):
        """Wait for the executor to finish the named parameter or action request."""
        return wait_future(future, timeout)
    acquisition = {}
    try:
        assert client.wait_for_server(timeout_sec=10)
        snapshot = acquire_inference(parameters, memory, wait, 10, acquisition)
        assert snapshot['run_id'] == memory.metadata['run_id']
        assert snapshot['loading']['model_id'] == 'fixture'
        assert memory.metadata['inference'] == snapshot
        verify_inference(parameters, memory, wait, 10)
        rejected = wait_future(client.send_goal_async(GenerateText.Goal(prompt='hello', run_id='')))
        assert not rejected.accepted
        owned = wait_future(client.send_goal_async(GenerateText.Goal(
            prompt='hello', run_id=memory.metadata['run_id'])))
        assert owned.accepted
        response = wait_future(owned.get_result_async())
        assert response.status == GoalStatus.STATUS_SUCCEEDED
        assert response.result.text == 'fixture output'
        changed = wait_future(parameters.set_parameters_atomically([Parameter('models.fixture.temperature', value=0.5)]))
        assert not changed.result.successful
        release_inference(parameters, memory.metadata['run_id'], wait, 10, {})
        assert server.get_parameter('experiment_run_id').value == ''
    finally:
        executor.shutdown(timeout_sec=10)
        thread.join(timeout=10)
        client.destroy()
        node.destroy_node()
        server.destroy_node()
        rclpy.shutdown()
