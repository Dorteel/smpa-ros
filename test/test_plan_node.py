"""Exercise the independent Plan action using mocked shared inference."""
import json
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import rclpy
from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.parameter import Parameter
import yaml

from memory import MemoryManager
from smpa_llm.server import InferenceServer
from smpa_ros.plan_node import PlanNode
from smpa_ros_interfaces.action import Plan


def test_plan_action_uses_shared_inference_and_persists_decisions(tmp_path):
    """Invoke Plan independently with real ROS actions and a weight-free backend.

    Start only Plan and the shared inference server. Check default and explicit
    capabilities, schema forwarding, run provenance and a rejected unsupported
    capability. Change the server's token budget and require Plan to abort and
    persist a failure instead of silently using different generation settings.
    """
    registry = tmp_path / 'models.yaml'
    registry.write_text(yaml.safe_dump({'models': {'fixture': {
        'backend': 'transformers', 'model_id': 'fixture', 'path': 'fixture'}}}))
    calls = []

    class Backend:
        """Return a validated planning decision without loading a model."""
        def generate(self, prompt, **settings):
            """Record the schema passed by the real server and return JSON."""
            calls.append((prompt, settings))
            return '{"action":"look_left"}'

    def loader(settings, model_root, configuration):
        """Create only a lightweight fixture backend for the selected alias."""
        return Backend()

    def wait(future):
        """Wait for an executor-driven ROS response and fail on timeout."""
        deadline = monotonic() + 5
        while not future.done() and monotonic() < deadline:
            sleep(0.01)
        assert future.done()
        return future.result()

    memory = MemoryManager(tmp_path, goal='Find a cup',
                           configuration={'pipeline': {'enabled_actions': ['look_left']}})
    memory.begin_cycle()
    rclpy.init()
    inference = InferenceServer(loader=loader, parameter_overrides=[Parameter('registry_file', value=str(registry))])
    inference.replace_model('fixture')
    plan = PlanNode(parameter_overrides=[Parameter('memory_root', value=str(tmp_path)),
                                         Parameter('timeout', value=5.0)])
    caller = rclpy.create_node('plan_test_client')
    client = ActionClient(caller, Plan, 'smpa/plan')
    executor = MultiThreadedExecutor(num_threads=2)
    for node in (inference, plan, caller):
        executor.add_node(node)
    pool = ThreadPoolExecutor(max_workers=1)
    spinning = pool.submit(executor.spin)
    try:
        assert client.wait_for_server(timeout_sec=5)
        for capability in ('', 'plan'):
            goal = Plan.Goal(run_id=memory.metadata['run_id'], capability=capability,
                             world_model='# World Model\nA cup is visible.')
            handle = wait(client.send_goal_async(goal))
            assert handle.accepted
            response = wait(handle.get_result_async())
            assert response.status == GoalStatus.STATUS_SUCCEEDED
            assert json.loads(response.result.action_json) == {'action': 'look_left'}
            record = json.loads((memory.directory / response.result.record_path).read_text())
            assert record['stage'] == 'plan' and record['status'] == 'generated'
            assert record['goal'] == 'Find a cup'
        assert len(calls) == 2 and all(item[1]['schema']['properties']['action']['enum'] == ['look_left'] for item in calls)
        rejected = wait(client.send_goal_async(Plan.Goal(run_id=memory.metadata['run_id'], capability='sense', world_model='x')))
        assert not rejected.accepted
        assert inference.set_parameters_atomically([Parameter('models.fixture.max_new_tokens', value=99)]).successful
        handle = wait(client.send_goal_async(Plan.Goal(run_id=memory.metadata['run_id'], world_model='x')))
        response = wait(handle.get_result_async())
        assert response.status == GoalStatus.STATUS_ABORTED
        assert json.loads((memory.directory / 'state/cycle_000001/plan_003.json').read_text())['status'] == 'failed'
        assert len(calls) == 2
    finally:
        client.destroy()
        executor.shutdown(wait_for_threads=True)
        spinning.result(timeout=5)
        pool.shutdown()
        plan.destroy_node()
        inference.destroy_node()
        caller.destroy_node()
        rclpy.shutdown()
