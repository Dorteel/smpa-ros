"""Exercise generated cognitive actions over ROS with mocked external operations."""
import json
from itertools import count
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


_TEST_DOMAINS = count(110)


@pytest.fixture(autouse=True)
def isolate_ros_domain(monkeypatch):
    """Give each transport test a fresh local DDS discovery domain.

    Stage processes inherit the same domain as their parent. A new domain for
    each test prevents recently stopped service endpoints from a previous ROS
    context from receiving a subsequent experiment's requests.
    """
    monkeypatch.setenv('ROS_DOMAIN_ID', str(next(_TEST_DOMAINS)))


def wait_future(future, timeout=10):
    """Wait for a ROS future completed by the executor and return its result.

    The test runs the executor in a separate thread. Wake when the given
    response finishes and fail instead of waiting indefinitely for discovery,
    goal acceptance, or an action result.
    """
    done = Event()
    def mark_complete(completed):
        """Wake the waiting test when this response finishes."""
        done.set()
    future.add_done_callback(mark_complete)
    if not done.wait(timeout):
        raise TimeoutError('ROS action response did not arrive')
    return future.result()


def fake_observation(*args, **kwargs):
    """Return one fixed observation without opening a sensor.

    Ignore the stage's operation context and return the same text and empty
    source path expected by the existing observation pipeline.
    """
    return '{"vision":"cup"}', ''


def fake_world_generation(*args, **kwargs):
    """Return a fixed world model without loading an inference model.

    Ignore generation controls because this fixture checks transport and
    persistence rather than model behavior.
    """
    return 'world with cup'


def fake_plan_generation(*args, **kwargs):
    """Return a valid robot decision without invoking model inference.

    The existing Plan implementation validates and persists this JSON before
    sending it to Act through the normal experiment sequence.
    """
    return '{"action":"look_left"}'


def fake_inference(*args, **kwargs):
    """Return fixed text for an accepted inference request without model weights.

    Keep the backend call deterministic so the test can check action ownership
    and result delivery without executing real inference.
    """
    return 'fixture output'


def fake_backend_loader(*args):
    """Create a weight-free backend for the actual inference server.

    The server still owns the backend and serializes its operations; this
    fixture only replaces the computational implementation.
    """
    from types import SimpleNamespace
    return SimpleNamespace(generate=fake_inference)


def test_real_action_transport_and_script_sequence(tmp_path, monkeypatch):
    """Send generated actions through four real ROS action servers.

    Construct independently owned stage nodes and a separate client node. Mock
    only sensor acquisition, model output, planning output and robot execution.
    Reject unsupported capabilities, then let the actual experiment script run
    two cycles using default and explicit capabilities. Check ROS terminal
    states, source records, shared run identity and completion. Invoke the same
    nodes in an alternative explicit order with selected prompts, then reject
    missing prompts. Check stage independence before draining
    and destroying every node and the ROS context.
    """
    import smpa_ros.sense_node as sense
    import smpa_ros.model_node as model
    import smpa_ros.plan_node as plan
    import smpa_ros.act_node as act
    monkeypatch.setattr(sense, 'observe', fake_observation)
    monkeypatch.setattr(model.ModelNode, 'generate', fake_world_generation)
    monkeypatch.setattr(plan.PlanNode, 'generate', fake_plan_generation)
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
            if len(order) > 8:
                goal.prompt_name = 'default'
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
        memory.begin_cycle()
        identity = memory.metadata['run_id']
        reordered = [('plan', Plan, Plan.Goal(run_id=identity, world_model='provided world')),
                     ('sense', Sense, Sense.Goal(run_id=identity)),
                     ('model', Model, Model.Goal(run_id=identity, observation='provided observation')),
                     ('act', Act, Act.Goal(run_id=identity, action_json='{"action":"look_left"}'))]
        for stage, kind, goal in reordered:
            invoke(stage, kind, goal)
        memory.complete_cycle()
        memory.in_cycle = False
        assert order[-4:] == ['plan', 'sense', 'model', 'act']
        assert memory.metadata['completed_cycles'] == 3
        third = memory.directory / 'state/cycle_000003'
        for stage in ('sense', 'model', 'plan', 'act'):
            record = json.loads((third / f'{stage}.json').read_text())
            assert record['prompt_name'] == 'default'
        memory.begin_cycle()
        missing = [('sense', Sense.Goal(run_id=identity, prompt_name='missing_prompt')),
                   ('model', Model.Goal(run_id=identity, prompt_name='missing_prompt', observation='seen')),
                   ('plan', Plan.Goal(run_id=identity, prompt_name='missing_prompt', world_model='world')),
                   ('act', Act.Goal(run_id=identity, prompt_name='missing_prompt', action_json='{\"action\":\"look_left\"}'))]
        for stage, goal in missing:
            handle = wait_future(clients[stage].send_goal_async(goal))
            assert handle.accepted
            assert wait_future(handle.get_result_async()).status == GoalStatus.STATUS_ABORTED
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


def stage_process(stage, root, stop, sensing_started=None, allow_sensing=None, camera=False):
    """Serve one stage in its own process with only external operations mocked.

    Select the named existing node, replace its external work with deterministic
    responses, and spin until the parent signals shutdown. Destroy the node and
    context so the parent can verify that each process terminates cleanly.
    """
    import smpa_ros.sense_node as sense
    import smpa_ros.model_node as model
    import smpa_ros.plan_node as plan
    import smpa_ros.act_node as act
    if camera and stage == 'sense':
        def describe(self, image, prompt, **context):
            """Return a fixed visual description without running image inference."""
            return 'A red pixel.'
        sense.SenseNode.describe = describe
    else:
        sense.observe = fake_observation
    if stage == 'sense' and sensing_started is not None:
        def delayed_observation(*args, **kwargs):
            """Hold one observation until the test allows the sensor to finish.

            Tell the parent that a stage is active, wait for its release, and
            then return the normal fixture observation without sensor access.
            """
            sensing_started.set()
            if not allow_sensing.wait(timeout=15):
                raise TimeoutError('Test did not release the mocked sensor')
            return fake_observation(*args, **kwargs)
        sense.observe = delayed_observation
    model.ModelNode.generate = fake_world_generation
    plan.PlanNode.generate = fake_plan_generation
    def dispatch(self, name, record, **context):
        """Return a successful robot result without sending a physical command."""
        record.update(goal_accepted=True, result_status=4, success=True)
    act.ActNode.dispatch = dispatch
    kinds = {'sense': sense.SenseNode, 'model': model.ModelNode,
             'plan': plan.PlanNode, 'act': act.ActNode}
    rclpy.init(args=[])
    node = kinds[stage](parameter_overrides=[Parameter('memory_root', value=root), Parameter('timeout', value=3.0)])
    executor = None
    thread = None
    try:
        if camera and stage == 'sense':
            executor = MultiThreadedExecutor(num_threads=2)
            executor.add_node(node)
            thread = Thread(target=executor.spin, daemon=True)
            thread.start()
            stop.wait()
        else:
            while not stop.is_set():
                rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        if executor is not None:
            executor.shutdown(timeout_sec=10)
            thread.join(timeout=10)
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
    server = InferenceServer(loader=fake_backend_loader,
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
    from rclpy.parameter_client import AsyncParameterClient
    from smpa_llm.server import InferenceServer
    from smpa_ros.smpa import acquire_inference, release_inference, verify_inference
    from smpa_ros_interfaces.action import GenerateText
    registry = tmp_path / 'models.yaml'
    registry.write_text(yaml.safe_dump({'models': {'fixture': {'backend': 'transformers', 'model_id': 'fixture', 'path': 'fake'}}}))
    memory = MemoryManager(tmp_path, goal='Find cup')
    rclpy.init(args=[])
    server = InferenceServer(loader=fake_backend_loader,
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


def test_installed_smpa_command_runs_complete_experiment(tmp_path):
    """Run the installed command against independent, mocked ROS participants.

    Start one real stage server per process and a real inference server with a
    weight-free backend. Run the installed console script with an API selection
    that names that fixture, then inspect the completed run, stage records and
    authoritative freeze. Stop and join every owned participant on all paths.
    """
    import multiprocessing
    import os
    import subprocess
    import yaml
    from ament_index_python.packages import get_package_prefix
    from smpa_llm.server import InferenceServer

    context = multiprocessing.get_context('spawn')
    stop = context.Event()
    names = ('sense', 'model', 'plan', 'act')
    processes = [context.Process(target=stage_process, args=(name, str(tmp_path), stop)) for name in names]
    for process in processes:
        process.start()
    registry = tmp_path / 'models.yaml'
    registry.write_text(yaml.safe_dump({'models': {'fixture': {'backend': 'transformers', 'model_id': 'fixture', 'path': 'fake'}}}))
    from ament_index_python.packages import get_package_share_directory
    configuration = yaml.safe_load((__import__('pathlib').Path(get_package_share_directory('smpa_ros')) / 'config.yaml').read_text())
    configuration['model'] = {'backend': 'api', 'provider': 'openai', 'model_id': 'fixture'}
    configuration['sensing'] = {'vision': False, 'hear': False}
    configuration.pop('scene_refinement', None)
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump(configuration))
    rclpy.init(args=[])
    server = InferenceServer(loader=fake_backend_loader,
                             parameter_overrides=[Parameter('registry_file', value=str(registry))])
    server.replace_model('fixture')
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(server)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    executable = __import__('pathlib').Path(get_package_prefix('smpa_ros')) / 'lib/smpa_ros/smpa'
    try:
        completed = subprocess.run([str(executable), '--config', str(config), '--world-model-root', str(tmp_path),
                                    '--max-cycles', '1', '--timeout', '15'], cwd=tmp_path,
                                   capture_output=True, text=True, timeout=90, env=os.environ.copy())
        assert completed.returncode == 0, completed.stdout + completed.stderr
        runs = list((tmp_path / 'runs').glob('*/metadata.yaml'))
        assert len(runs) == 1
        metadata = yaml.safe_load(runs[0].read_text())
        assert metadata['status'] == 'completed' and metadata['completed_cycles'] == 1
        assert metadata['inference']['run_id'] == metadata['run_id']
        assert metadata['inference']['loading']['model_id'] == 'fixture'
        directory = runs[0].parent / 'state/cycle_000001'
        records = [json.loads((directory / f'{stage}.json').read_text()) for stage in names]
        assert [item['sequence'] for item in records] == [1, 2, 3, 4]
        assert server.get_parameter('experiment_run_id').value == ''
    finally:
        executor.shutdown(timeout_sec=10)
        thread.join(timeout=10)
        server.destroy_node()
        rclpy.shutdown()
        stop.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        assert all(process.exitcode == 0 for process in processes)


def test_installed_smpa_command_interrupts_and_releases_freeze(tmp_path):
    """Interrupt the installed command while a mocked Sense action is active.

    Wait until the real Sense action reaches a held observation, signal the
    experiment, then allow that action to finish. Confirm the run is marked
    interrupted, the active inference owner is released only after completion,
    and every independently launched stage process terminates cleanly.
    """
    import multiprocessing
    import os
    from pathlib import Path
    import signal
    import subprocess
    import yaml
    from ament_index_python.packages import get_package_prefix, get_package_share_directory
    from smpa_llm.server import InferenceServer

    context = multiprocessing.get_context('spawn')
    stop, sensing_started, allow_sensing = context.Event(), context.Event(), context.Event()
    names = ('sense', 'model', 'plan', 'act')
    processes = [context.Process(target=stage_process,
        args=(name, str(tmp_path), stop, sensing_started if name == 'sense' else None, allow_sensing))
        for name in names]
    for process in processes:
        process.start()
    registry = tmp_path / 'models.yaml'
    registry.write_text(yaml.safe_dump({'models': {'fixture': {'backend': 'transformers', 'model_id': 'fixture', 'path': 'fake'}}}))
    configuration = yaml.safe_load((Path(get_package_share_directory('smpa_ros')) / 'config.yaml').read_text())
    configuration['model'] = {'backend': 'api', 'provider': 'openai', 'model_id': 'fixture'}
    configuration['sensing'] = {'vision': False, 'hear': False}
    configuration.pop('scene_refinement', None)
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump(configuration))
    rclpy.init(args=[])
    server = InferenceServer(loader=fake_backend_loader,
                             parameter_overrides=[Parameter('registry_file', value=str(registry))])
    server.replace_model('fixture')
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(server)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    executable = Path(get_package_prefix('smpa_ros')) / 'lib/smpa_ros/smpa'
    process = None
    try:
        process = subprocess.Popen([str(executable), '--config', str(config), '--world-model-root', str(tmp_path),
                                    '--max-cycles', '1', '--timeout', '15'], cwd=tmp_path,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
        assert sensing_started.wait(timeout=30), 'Sense action did not begin'
        process.send_signal(signal.SIGINT)
        allow_sensing.set()
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode != 0, stdout + stderr
        metadata_path, = (tmp_path / 'runs').glob('*/metadata.yaml')
        metadata = yaml.safe_load(metadata_path.read_text())
        assert metadata['status'] == 'interrupted'
        assert metadata['completed_cycles'] == 0
        assert server.get_parameter('experiment_run_id').value == ''
    finally:
        allow_sensing.set()
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
        executor.shutdown(timeout_sec=10)
        thread.join(timeout=10)
        server.destroy_node()
        rclpy.shutdown()
        stop.set()
        for child in processes:
            child.join(timeout=10)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        assert all(child.exitcode == 0 for child in processes)


def test_installed_experiment_preserves_camera_evidence(tmp_path):
    """Persist mocked camera frames through the installed cognitive experiment.

    Start the four stage processes and the weight-free inference server. Publish
    an actual ROS image while only the visual description, language generation,
    planning and robot movement are mocked. Run two cycles through the installed
    command and verify the original image and Markdown evidence, Model source
    links, completed cycles, and cleanup of all owned participants.
    """
    import multiprocessing
    import os
    from pathlib import Path
    import subprocess
    import yaml
    from ament_index_python.packages import get_package_prefix, get_package_share_directory
    from PIL import Image as PILImage
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image
    from smpa_llm.server import InferenceServer

    context = multiprocessing.get_context('spawn')
    stop = context.Event()
    names = ('sense', 'model', 'plan', 'act')
    processes = [context.Process(target=stage_process,
        args=(name, str(tmp_path), stop, None, None, name == 'sense')) for name in names]
    for process in processes:
        process.start()
    registry = tmp_path / 'models.yaml'
    registry.write_text(yaml.safe_dump({'models': {'fixture': {'backend': 'transformers', 'model_id': 'fixture', 'path': 'fake'}}}))
    configuration = yaml.safe_load((Path(get_package_share_directory('smpa_ros')) / 'config.yaml').read_text())
    configuration['model'] = {'backend': 'api', 'provider': 'openai', 'model_id': 'fixture'}
    configuration['sensing'] = {'vision': True, 'hear': False}
    configuration['camera_topic'] = '/test/cognitive_camera'
    configuration.pop('scene_refinement', None)
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump(configuration))
    rclpy.init(args=[])
    server = InferenceServer(loader=fake_backend_loader,
                             parameter_overrides=[Parameter('registry_file', value=str(registry))])
    server.replace_model('fixture')
    camera = Node('mock_camera')
    publisher = camera.create_publisher(Image, configuration['camera_topic'], qos_profile_sensor_data)
    frame = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
    def publish_frame():
        """Publish one deterministic fixture image without connecting hardware."""
        publisher.publish(frame)
    camera.create_timer(0.05, publish_frame)
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(server)
    executor.add_node(camera)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    executable = Path(get_package_prefix('smpa_ros')) / 'lib/smpa_ros/smpa'
    try:
        completed = subprocess.run([str(executable), '--config', str(config), '--world-model-root', str(tmp_path),
                                    '--max-cycles', '2', '--timeout', '15'], cwd=tmp_path,
                                   capture_output=True, text=True, timeout=90, env=os.environ.copy())
        assert completed.returncode == 0, completed.stdout + completed.stderr
        metadata_path, = (tmp_path / 'runs').glob('*/metadata.yaml')
        metadata = yaml.safe_load(metadata_path.read_text())
        assert metadata['status'] == 'completed' and metadata['completed_cycles'] == 2
        directory = metadata_path.parent
        observations = list((directory / 'observations/vision').glob('*.md'))
        assert len(observations) == 2
        for observation in observations:
            assert observation.read_text() == 'A red pixel.'
            with PILImage.open(observation.with_suffix('.png')) as image:
                assert image.getpixel((0, 0)) == (255, 0, 0)
        for cycle in (1, 2):
            record = json.loads((directory / f'state/cycle_{cycle:06d}/model.json').read_text())
            assert record['source_observations'][0] == f'state/cycle_{cycle:06d}/sense.json'
            assert {Path(item).suffix for item in record['source_observations'][1:]} == {'.md', '.png'}
            assert (directory / f'world_model/iteration_{cycle:03d}.md').exists()
    finally:
        executor.shutdown(timeout_sec=10)
        thread.join(timeout=10)
        camera.destroy_node()
        server.destroy_node()
        rclpy.shutdown()
        stop.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        assert all(process.exitcode == 0 for process in processes)
