"""Run the production lifecycle worker with mocked cognitive action clients."""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from memory import MemoryManager
from smpa_ros import smpa
from smpa_ros.run_control import run_worker
from smpa_ros_interfaces import action


def run_fixture(arguments, *, goal, notify):
    """Run the real script lifecycle using deterministic mock stage responses.

    Persist a run under the requested root and supply a fake inference snapshot.
    A goal containing WAIT blocks in the mocked Sense send until SIGINT so the
    lifecycle service test can verify interruption and cleanup. No ROS stage,
    inference model, audio hardware or simulated robot is started here.
    """
    for name in ('Sense', 'Model', 'Plan', 'Act'):
        setattr(action, name, SimpleNamespace(Goal=SimpleNamespace))
    root = Path(arguments[arguments.index('--world-model-root') + 1])
    memory = MemoryManager(root, goal=goal)
    identity = memory.metadata['run_id']
    snapshot = dict(run_id=identity, server_instance_id='fixture', active_model='fixture', loading={}, generation={})
    parameters = SimpleNamespace(wait_for_services=lambda timeout_sec: True,
        set_parameters_atomically=lambda values: SimpleNamespace(result=SimpleNamespace(successful=True, reason='')),
        get_parameters=lambda names: SimpleNamespace(values=[SimpleNamespace(string_value=json.dumps(snapshot))]))
    responses = {'sense': SimpleNamespace(observation='seen', record_path='sense'),
                 'model': SimpleNamespace(world_model='world', record_path='model', snapshot_path='snapshot'),
                 'plan': SimpleNamespace(action_json='{"action":"look_left"}', record_path='plan'),
                 'act': SimpleNamespace(success=True)}
    clients = {}
    for stage in responses:
        def send(request, stage=stage):
            """Return a fixed stage result or wait for the test's stop signal."""
            if stage == 'sense' and 'WAIT' in goal:
                time.sleep(60)
            return SimpleNamespace(accepted=True, get_result_async=lambda: SimpleNamespace(status=4, result=responses[stage]))
        clients[stage] = SimpleNamespace(wait_for_server=lambda timeout_sec: True, send_goal_async=send)
    def wait(future, timeout, operation):
        """Return an already completed mock ROS response."""
        return future
    def relay(stage, event):
        """Attach the persisted run identity to a lifecycle notification."""
        notify(identity, stage, event)
    smpa.execute_experiment(memory, 1, clients, parameters, wait, 1.0, notify=relay)


if __name__ == '__main__':
    smpa.run_ros_experiment = run_fixture
    run_worker(int(sys.argv[1]), sys.argv[2], sys.argv[3:])
