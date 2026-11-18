"""Check the opt-in inference adapter without loading real model weights."""
from types import SimpleNamespace
from threading import Event, Thread

import pytest
import rclpy
from rclpy.action import CancelResponse, GoalResponse
from rclpy.parameter import Parameter
import yaml
from smpa_llm.server import InferenceServer
from smpa_ros_interfaces.action import LoadModel, UnloadModel
from smpa_ros_interfaces.srv import ListModels, GetModelStatus


class FakeGoal:
    """Record native action outcomes without sending a network request."""

    def __init__(self, **fields):
        """Retain goal fields and an initially unset terminal action state.

        Tests inspect feedback and outcome after invoking the real callback.
        No fake result is used to validate real model output.
        """
        self.request = SimpleNamespace(**fields)
        self.outcome = None
        self.feedback = []

    def publish_feedback(self, value):
        """Record actual adapter feedback for assertions after completion.

        Store the message unchanged so tests can reject simulated text streaming.
        """
        self.feedback.append(value)

    def succeed(self):
        """Record that the adapter selected the native success state.

        The test still checks the returned result and the real server state.
        """
        self.outcome = 'succeeded'

    def abort(self):
        """Record the native abort state selected for a failed operation.

        This lets tests distinguish an explicit failure from a successful empty result.
        """
        self.outcome = 'aborted'


@pytest.fixture
def server(tmp_path):
    """Create a real ROS node with a small registered catalogue and an injected loader.

    Use distinct Qwen and Llama aliases solely as fixtures, never downloading their
    weights. Track constructor calls and permit a deliberate failed load. Destroy
    the node and its context after every test so DDS handles cannot leak.
    """
    registry = tmp_path / 'models.yaml'
    registry.write_text(yaml.safe_dump({'models': {
        'qwen3_4b': {'backend': 'transformers', 'model_id': 'fixture/qwen', 'path': 'qwen'},
        'llama3_8b': {'backend': 'transformers', 'model_id': 'fixture/llama', 'path': 'llama'},
    }}))
    calls = []

    def load(values, model_root, configuration):
        """Record loading snapshots and return a controllable weight-free backend.

        A deliberate broken identity fails before returning an instance. Normal
        loads return only metadata; inference-specific tests replace the callable.
        """
        calls.append(dict(values))
        if values['model_id'] == 'broken':
            raise RuntimeError('fixture load failure')
        return SimpleNamespace(identity=values['model_id'])

    rclpy.init()
    node = InferenceServer(loader=load, parameter_overrides=[Parameter('registry_file', value=str(registry))])
    node.load_calls = calls
    try:
        yield node
    finally:
        node.destroy_node()
        rclpy.shutdown()


def test_model_specific_parameters_and_validation(server):
    """Change Qwen sampling atomically while preserving the Llama namespace.

    Verify defaults are declared, invalid ranges and undeclared parameters fail,
    and direct active-model edits cannot advertise an unloaded selection.
    """
    result = server.set_parameters_atomically([
        Parameter('models.qwen3_4b.temperature', value=0.7),
        Parameter('models.qwen3_4b.max_new_tokens', value=512)])
    assert result.successful
    assert server.settings('qwen3_4b')['temperature'] == 0.7
    assert server.settings('llama3_8b')['temperature'] == 0.0
    assert not server.set_parameters_atomically([Parameter('models.qwen3_4b.temperature', value=3.0)]).successful
    assert not server.set_parameters_atomically([Parameter('active_model', value='qwen3_4b')]).successful
    with pytest.raises(rclpy.exceptions.ParameterNotDeclaredException):
        server.set_parameters_atomically([Parameter('undeclared', value=1)])


def test_loading_staging_replacement_and_recovery(server):
    """Reuse matching loads, stage loading edits, then recover from replacement failure.

    A generation edit never triggers reloading. A model-id edit is applied only by
    explicit management. Failed replacement must clear the active identity and free
    the operation for retry; unloading leaves a genuinely empty server.
    """
    server.replace_model('qwen3_4b')
    first = server.backend
    server.replace_model('qwen3_4b')
    assert server.backend is first and len(server.load_calls) == 1
    server.set_parameters_atomically([Parameter('models.qwen3_4b.temperature', value=0.4)])
    server.replace_model('qwen3_4b')
    assert len(server.load_calls) == 1
    server.set_parameters_atomically([Parameter('models.qwen3_4b.model_id', value='broken')])
    assert server.backend is first
    assert server.reserve(None) == GoalResponse.ACCEPT
    goal = FakeGoal(model_id='qwen3_4b')
    result = server.manage(goal, action_type=LoadModel, operation='load_model')
    assert not result.success and goal.outcome == 'aborted'
    assert server.backend is None and server.state == 'error' and server.active_alias == ''
    assert not server.busy
    server.replace_model('llama3_8b')
    assert server.state == 'ready' and server.active_alias == 'llama3_8b'
    assert server.get_parameter('active_model').value == 'llama3_8b'
    server.release_model()
    assert server.state == 'unloaded' and server.backend is None


def test_exclusive_operations_and_noncooperative_cancel(server):
    """Reject overlapping access and cancellation unsupported by existing backends.

    Management and inference share a reservation. A rejected cancellation must
    leave the active operation owned until execute cleanup releases it.
    """
    assert server.reserve(None) == GoalResponse.ACCEPT
    assert server.reserve(None) == GoalResponse.REJECT
    assert server.cancel(FakeGoal()) == CancelResponse.REJECT
    goal = FakeGoal()
    assert server.manage(goal, action_type=UnloadModel, operation='unload_model').success
    assert not server.busy and goal.outcome == 'succeeded'


def test_status_and_catalogue_do_not_load_models(server):
    """Inspect declared catalogue and unloaded status without creating a backend.

    Require retained reliable status QoS for late subscribers and ensure short
    discovery services have no model-loading side effects.
    """
    from rclpy.qos import DurabilityPolicy, ReliabilityPolicy
    response = server.list_models(ListModels.Request(), ListModels.Response())
    assert {'qwen3_4b', 'llama3_8b'} <= set(response.model_ids)
    status = server.get_status(GetModelStatus.Request(), GetModelStatus.Response())
    assert (status.state, status.active_model) == ('unloaded', '')
    qos = server.status_publisher.qos_profile
    assert qos.durability == DurabilityPolicy.TRANSIENT_LOCAL
    assert qos.reliability == ReliabilityPolicy.RELIABLE
    assert not server.load_calls


def test_explicit_initial_selection_and_overrides(server):
    """Apply declared ROS overrides before loading the explicitly selected alias.

    Create another node in the same context with fixture assets. Its selected
    model must be ready, with Qwen settings overridden independently of Llama.
    Destroy the additional node even if an assertion fails.
    """
    node = InferenceServer(loader=server.loader, parameter_overrides=[
        Parameter('registry_file', value=server.get_parameter('registry_file').value),
        Parameter('active_model', value='qwen3_4b'),
        Parameter('models.qwen3_4b.temperature', value=0.7)])
    try:
        assert node.state == 'ready' and node.active_alias == 'qwen3_4b'
        assert node.settings('qwen3_4b')['temperature'] == 0.7
        assert node.settings('llama3_8b')['temperature'] == 0.0
    finally:
        node.destroy_node()
