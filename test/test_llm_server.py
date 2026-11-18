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


def test_generation_schema_and_image_contracts(server):
    """Exercise minimal requests against backend methods that record actual controls.

    Reuse one loaded fixture for plain text, constrained JSON and image inference.
    Require schema forwarding, output validation, original RGB pixels and no fake
    streaming. Invalid schemas, unsupported images and malformed generated JSON
    must abort rather than return successful unconstrained output.
    """
    from smpa_ros_interfaces.action import GenerateText, GenerateStructured, DescribeImage
    from sensor_msgs.msg import Image
    calls = []

    def generate(prompt, **options):
        """Record text controls and return fixture text or schema-compliant JSON.

        The adapter must pass the schema to this callable instead of performing
        an unconstrained call and merely validating after completion.
        """
        calls.append((prompt, options))
        return '{"answer": "yes"}' if 'schema' in options else 'fixture answer'

    def describe(image, prompt, **options):
        """Require preserved image pixels and record delegated vision controls.

        Return a fixture description without loading a second model instance.
        """
        assert image.getpixel((0, 0)) == (255, 0, 0)
        calls.append((prompt, options))
        return 'red pixel'

    server.replace_model('qwen3_4b')
    server.backend.generate = generate
    server.backend.describe = describe
    goal = FakeGoal(prompt='hello')
    assert server.reserve(None) == GoalResponse.ACCEPT
    assert server.infer(goal, action_type=GenerateText, result_field='text').text == 'fixture answer'
    assert goal.outcome == 'succeeded' and not goal.feedback
    schema = '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]}'
    goal = FakeGoal(prompt='answer', json_schema=schema)
    assert server.infer(goal, action_type=GenerateStructured, result_field='json_output').json_output
    assert calls[-1][1]['schema']['required'] == ['answer']
    image = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
    goal = FakeGoal(prompt='describe', image=image)
    assert server.infer(goal, action_type=DescribeImage, result_field='description').description == 'red pixel'
    goal = FakeGoal(prompt='bad', json_schema='{"type":"made_up"}')
    before = len(calls)
    server.infer(goal, action_type=GenerateStructured, result_field='json_output')
    assert goal.outcome == 'aborted' and len(calls) == before
    del server.backend.describe
    goal = FakeGoal(prompt='describe', image=image)
    server.infer(goal, action_type=DescribeImage, result_field='description')
    assert goal.outcome == 'aborted'
    assert len(server.load_calls) == 1


def test_request_snapshot_and_busy_switch_safety(server):
    """Update sampling while generation is blocked without changing that request.

    An actual worker thread holds backend access. Verify status remains readable,
    loading edits remain staged, switching is rejected and the current request
    retains its original controls. A later request can use the new values.
    """
    from smpa_ros_interfaces.action import GenerateText
    entered, finish = Event(), Event()
    calls = []

    def generate(prompt, **settings):
        """Record the captured request and wait for the test to release inference.

        Do not return until concurrent parameter/status checks have completed.
        This models a blocking backend without permitting simultaneous loading.
        """
        calls.append(settings)
        entered.set()
        assert finish.wait(5)
        return 'done'

    server.replace_model('qwen3_4b')
    server.backend.generate = generate
    goal = FakeGoal(prompt='snapshot')
    server.reserve(None)
    worker = Thread(target=server.infer, args=(goal,), kwargs={'action_type': GenerateText, 'result_field': 'text'})
    worker.start()
    try:
        assert entered.wait(5)
        assert server.get_status(GetModelStatus.Request(), GetModelStatus.Response()).state == 'generating'
        assert server.reserve(None) == GoalResponse.REJECT
        assert server.set_parameters_atomically([
            Parameter('models.qwen3_4b.temperature', value=0.8),
            Parameter('models.qwen3_4b.model_id', value='staged')]).successful
        assert calls[0]['temperature'] == 0.0
        assert server.loaded_settings['model_id'] == 'fixture/qwen'
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive() and goal.outcome == 'succeeded'
    assert len(server.load_calls) == 1


def test_unsupported_text_backend_aborts_without_inference(server):
    """Reject text and structured requests when the loaded backend is vision-only.

    A backend exposing only image description cannot generate text or enforce a
    text schema. Both goals must abort and leave the backend available for a
    later supported request, without trying to call a missing method.
    """
    from smpa_ros_interfaces.action import GenerateText, GenerateStructured
    server.replace_model('qwen3_4b')
    server.backend.describe = lambda *args, **kwargs: 'image'
    for action, fields, result_field in (
            (GenerateText, {'prompt': 'hello'}, 'text'),
            (GenerateStructured, {'prompt': 'hello', 'json_schema': '{"type":"object"}'}, 'json_output')):
        goal = FakeGoal(**fields)
        server.reserve(None)
        server.infer(goal, action_type=action, result_field=result_field)
        assert goal.outcome == 'aborted'
        assert server.backend is not None and not server.busy


def test_catalogue_excludes_unsupported_backends(server):
    """List only aliases whose current adapter can own the model lifecycle.

    Keep the registered entry and its parameters available, but do not advertise
    a specialized engine as loadable. Changing an alias to an unsupported backend
    must be reflected in the next discovery response without loading weights.
    """
    assert server.set_parameters_atomically([
        Parameter('models.qwen3_4b.backend', value='faster_whisper')]).successful
    response = server.list_models(ListModels.Request(), ListModels.Response())
    assert 'qwen3_4b' not in response.model_ids
    assert 'llama3_8b' in response.model_ids
    assert not server.load_calls


def test_catalogue_excludes_missing_local_path(server):
    """Exclude a local selection that lacks a path required by its loader.

    Keep the other registered selection visible and avoid any model loading.
    The blank path cannot identify a local checkpoint for this server.
    """
    assert server.set_parameters_atomically([
        Parameter('models.qwen3_4b.path', value='')]).successful
    response = server.list_models(ListModels.Request(), ListModels.Response())
    assert 'qwen3_4b' not in response.model_ids
    assert 'llama3_8b' in response.model_ids
    assert not server.load_calls


def test_structured_image_delegates_schema_and_rejects_invalid_output(server):
    """Forward an image and actual schema to constrained vision generation.

    The fake vision backend records the schema, response format and original
    pixel without loading weights. Valid output succeeds; a schema-invalid result
    and an invalid schema abort without claiming unconstrained generation worked.
    """
    from smpa_ros_interfaces.action import DescribeStructuredImage
    from sensor_msgs.msg import Image
    calls = []
    output = ['{"answer":"yes"}']

    def describe(image, prompt, **settings):
        """Record image and generation settings, then return the selected fixture."""
        calls.append((image.getpixel((0, 0)), prompt, settings))
        return output[0]

    server.replace_model('qwen3_4b')
    server.backend.describe = describe
    schema = '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]}'
    image = Image(width=1, height=1, encoding='rgb8', step=3, data=[255, 0, 0])
    goal = FakeGoal(image=image, prompt='What is visible?', json_schema=schema)
    server.reserve(None)
    result = server.infer(goal, action_type=DescribeStructuredImage, result_field='json_output')
    assert result.json_output == output[0] and goal.outcome == 'succeeded'
    assert calls[0][0] == (255, 0, 0)
    assert calls[0][2]['schema']['required'] == ['answer']
    assert calls[0][2]['response_format'] == 'json_schema'
    assert not goal.feedback
    output[0] = '{"answer":42}'
    goal = FakeGoal(image=image, prompt='What is visible?', json_schema=schema)
    server.reserve(None)
    server.infer(goal, action_type=DescribeStructuredImage, result_field='json_output')
    assert goal.outcome == 'aborted'
    goal = FakeGoal(image=image, prompt='What is visible?', json_schema='{"type":"invalid"}')
    before = len(calls)
    server.reserve(None)
    server.infer(goal, action_type=DescribeStructuredImage, result_field='json_output')
    assert goal.outcome == 'aborted' and len(calls) == before


def test_experiment_freezes_configuration_and_management(server):
    """Freeze a ready mocked model, reject changes, and release it explicitly.

    An unloaded or busy server cannot be acquired. Once a run owns a ready
    server, model settings and model-management goals are rejected while normal
    inference can still reserve the backend. Releasing the run restores edits.
    No real model weights are loaded.
    """
    run = Parameter('experiment_run_id', value='run-a')
    release = Parameter('experiment_run_id', value='release:run-a')
    temperature = Parameter('models.qwen3_4b.temperature', value=0.4)
    assert not server.set_parameters_atomically([run]).successful
    server.replace_model('qwen3_4b')
    assert server.reserve(None) == GoalResponse.ACCEPT
    assert not server.set_parameters_atomically([run]).successful
    server.busy = False
    assert not server.set_parameters_atomically([run, temperature]).successful
    assert server.set_parameters_atomically([run]).successful
    assert not server.set_parameters_atomically([temperature]).successful
    assert not server.set_parameters_atomically([Parameter('experiment_run_id', value='run-b')]).successful
    assert server.reserve(None, management=True) == GoalResponse.REJECT
    assert server.reserve(None) == GoalResponse.ACCEPT
    assert not server.set_parameters_atomically([release]).successful
    server.busy = False
    assert server.set_parameters_atomically([release]).successful
    assert server.set_parameters_atomically([temperature]).successful


def test_experiment_rejects_staged_loading_settings(server):
    """Require a reload before freezing a model whose loading settings changed.

    The frozen parameter values must identify the actual loaded weights, rather
    than a staged configuration that only applies to a future model load.
    """
    server.replace_model('qwen3_4b')
    assert server.set_parameters_atomically([Parameter('models.qwen3_4b.model_id', value='staged')]).successful
    assert not server.set_parameters_atomically([Parameter('experiment_run_id', value='run-a')]).successful


def test_run_scoped_inference_requires_matching_owner(server):
    """Check run ownership atomically with the inference reservation.

    A run-scoped goal is rejected while unloaded, after ownership changes, or
    after release. A matching goal is accepted and prevents ownership changes
    until its operation releases the slot. An empty ID is rejected while frozen
    and preserves standalone use after the run releases the server.
    No model weights or inference are used by this test.
    """
    run_a = SimpleNamespace(run_id='run-a')
    run_b = SimpleNamespace(run_id='run-b')
    standalone = SimpleNamespace(run_id='')
    assert server.reserve(run_a, inference=True) == GoalResponse.REJECT
    server.replace_model('qwen3_4b')
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='run-a')]).successful
    assert server.reserve(run_b, inference=True) == GoalResponse.REJECT
    assert server.reserve(standalone, inference=True) == GoalResponse.REJECT
    assert server.reserve(run_a, inference=True) == GoalResponse.ACCEPT
    assert not server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-a')]).successful
    server.busy = False
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-a')]).successful
    assert server.reserve(run_a, inference=True) == GoalResponse.REJECT
    assert server.reserve(standalone, inference=True) == GoalResponse.ACCEPT
    server.busy = False


def test_experiment_release_checks_owner_atomically(server):
    """Reject a bare or stale release without disturbing a newer experiment.

    Release the first owner's freeze using its identifier, then acquire for a
    second run. A delayed release for the first run must not clear the second
    owner's parameter. No model weights or inference are used.
    """
    server.replace_model('qwen3_4b')
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='run-a')]).successful
    assert not server.set_parameters_atomically([Parameter('experiment_run_id', value='')]).successful
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-a')]).successful
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='run-b')]).successful
    assert not server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-a')]).successful
    assert not server.set_parameters([Parameter('experiment_run_id', value='')])[0].successful
    assert not server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-b'),
                                                  Parameter('experiment_run_id', value='run-c')]).successful
    assert server.get_parameter('experiment_run_id').value == 'run-b'
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-b')]).successful


def test_shutdown_releases_owned_freeze_after_callbacks_drain(server):
    """Release an idle experiment freeze when its inference server shuts down.

    Use a second mocked server in the existing ROS context. Destruction must
    clear ownership and unload its backend without relying on the experiment to
    have sent a separate release request. No model weights are loaded.
    """
    registry = server.get_parameter('registry_file').value
    other = InferenceServer(loader=lambda values, root, config: SimpleNamespace(),
                            parameter_overrides=[Parameter('registry_file', value=registry)])
    other.replace_model('qwen3_4b')
    assert other.set_parameters_atomically([Parameter('experiment_run_id', value='run-a')]).successful
    other.destroy_node()
    assert other.backend is None

def test_server_captures_read_only_experiment_snapshot(server):
    """Expose the actual loaded settings while an experiment owns the server.

    Acquire a mock backend after configuring its generation controls. The
    read-only snapshot must match the loaded identity and frozen generation;
    release clears it. Clients cannot replace the snapshot themselves.
    """
    import json
    server.replace_model('qwen3_4b')
    assert server.set_parameters_atomically([Parameter('models.qwen3_4b.temperature', value=0.4)]).successful
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='run-a')]).successful
    snapshot = json.loads(server.get_parameter('frozen_settings_json').value)
    assert snapshot['run_id'] == 'run-a' and snapshot['active_model'] == 'qwen3_4b'
    assert snapshot['server_instance_id']
    assert snapshot['loading']['model_id'] == 'fixture/qwen'
    assert snapshot['generation']['temperature'] == 0.4
    assert not server.set_parameters_atomically([Parameter('frozen_settings_json', value='{}')]).successful
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-a')]).successful
    assert server.get_parameter('frozen_settings_json').value == ''


def test_snapshot_distinguishes_server_restart(server):
    """Give each server instance a distinct identity in its frozen snapshot.

    Freeze two mocked servers using the same run ID and loading configuration.
    Their instance IDs must differ, allowing the experiment to detect that the
    original server restarted even when the visible model settings match.
    """
    import json
    registry = server.get_parameter('registry_file').value
    other = InferenceServer(loader=lambda values, root, config: SimpleNamespace(),
                            parameter_overrides=[Parameter('registry_file', value=registry)])
    try:
        for node in (server, other):
            node.replace_model('qwen3_4b')
            assert node.set_parameters_atomically([Parameter('experiment_run_id', value='run-a')]).successful
        first = json.loads(server.get_parameter('frozen_settings_json').value)
        second = json.loads(other.get_parameter('frozen_settings_json').value)
        assert first['server_instance_id'] != second['server_instance_id']
        assert server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-a')]).successful
    finally:
        other.destroy_node()


def test_experiment_persists_real_server_snapshot_with_mock_backend(server, tmp_path):
    """Exercise acquisition, metadata and release against the real server object.

    Replace only ROS transport with direct calls and use the fixture's mock model
    loader. The experiment must persist the exact server snapshot, verify it,
    and release the matching run owner without loading weights or inference.
    """
    import json
    import yaml
    from memory import MemoryManager
    from smpa_ros.smpa import acquire_inference, verify_inference, release_inference
    server.replace_model('qwen3_4b')
    memory = MemoryManager(tmp_path, goal='Test ownership', models={'model': {'model_id': 'fixture/qwen'}})
    def set_parameters(values):
        """Return the real server's atomic parameter result in a ROS-shaped reply."""
        return SimpleNamespace(result=server.set_parameters_atomically(values))
    def get_parameters(names):
        """Read the real server's ROS parameter values without network transport."""
        return SimpleNamespace(values=[server.get_parameter(name).get_parameter_value() for name in names])
    parameters = SimpleNamespace(wait_for_services=lambda timeout_sec: True,
                                 set_parameters_atomically=set_parameters, get_parameters=get_parameters)
    def wait(future, timeout, operation):
        """Return each completed direct-call response without starting an executor."""
        return future
    acquisition = {}
    snapshot = acquire_inference(parameters, memory, wait, 1.0, acquisition)
    assert snapshot == json.loads(server.get_parameter('frozen_settings_json').value)
    assert yaml.safe_load((memory.directory / 'metadata.yaml').read_text())['inference'] == snapshot
    verify_inference(parameters, memory, wait, 1.0)
    release_inference(parameters, memory.metadata['run_id'], wait, 1.0, {})
    assert server.get_parameter('experiment_run_id').value == ''


def test_freeze_snapshot_captures_actual_model_runtime(server):
    """Capture actual device and dtype from the one loaded backend on acquisition.

    Give the fake backend runtime values different from its configured defaults,
    then verify the frozen snapshot reports those actual values. Release the
    owner and leave backend cleanup to the existing server fixture.
    """
    import json
    server.replace_model('qwen3_4b')
    server.backend.model = SimpleNamespace(device='fixture-device', dtype='fixture-dtype')
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='run-a')]).successful
    snapshot = json.loads(server.get_parameter('frozen_settings_json').value)
    assert snapshot['runtime'] == {'device': 'fixture-device', 'dtype': 'fixture-dtype'}
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-a')]).successful


def test_stage_specific_settings_are_frozen_and_forwarded(server):
    """Freeze distinct stage settings and forward only the selected stage's values.

    Use one fake API backend and a single model instance. Configure Model and
    Plan independently, acquire one run, and reject edits while it owns the
    server. Check the recorded snapshot and the exact options sent to the
    backend; unknown stages and other runs must not reserve an operation.
    """
    import json
    from smpa_ros_interfaces.action import GenerateText
    assert server.set_parameters_atomically([Parameter('models.qwen3_4b.backend', value='api')]).successful
    server.replace_model('qwen3_4b')
    calls = []
    def generate(prompt, **options):
        """Record the selected generation controls without contacting a model."""
        calls.append((prompt, options))
        return 'fixture response'
    def generate_conversation(prompt, **options):
        """Return the same recorded response through the API chat entry point."""
        return {'response': generate(prompt, **options)}
    server.backend.generate = generate
    server.backend.generate_conversation = generate_conversation
    updates = [Parameter('models.qwen3_4b.stages.model.max_new_tokens', value=41),
               Parameter('models.qwen3_4b.stages.model.reasoning_effort', value='low'),
               Parameter('models.qwen3_4b.stages.model.top_p', value=0.8),
               Parameter('models.qwen3_4b.stages.plan.max_new_tokens', value=83),
               Parameter('models.qwen3_4b.stages.plan.reasoning_effort', value='high'),
               Parameter('models.qwen3_4b.stages.plan.top_p', value=0.9)]
    assert server.set_parameters_atomically(updates).successful
    assert server.stage_settings('llama3_8b', 'model')['max_new_tokens'] == 128
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='run-a')]).successful
    snapshot = json.loads(server.get_parameter('frozen_settings_json').value)
    assert snapshot['stages']['model']['max_new_tokens'] == 41
    assert snapshot['stages']['model']['reasoning_effort'] == 'low'
    assert snapshot['stages']['plan']['max_new_tokens'] == 83
    assert snapshot['stages']['plan']['reasoning_effort'] == 'high'
    assert not server.set_parameters_atomically([updates[0]]).successful
    assert server.reserve(SimpleNamespace(run_id='run-a', stage='unknown'), inference=True) == GoalResponse.REJECT
    assert server.reserve(SimpleNamespace(run_id='wrong', stage='model'), inference=True) == GoalResponse.REJECT
    for stage in ('model', 'plan'):
        goal = FakeGoal(prompt='hello', run_id='run-a', stage=stage)
        assert server.reserve(goal.request, inference=True) == GoalResponse.ACCEPT
        result = server.infer(goal, action_type=GenerateText, result_field='text')
        assert goal.outcome == 'succeeded' and result.text == 'fixture response'
    assert calls[0][1]['max_new_tokens'] == 41 and calls[0][1]['reasoning_effort'] == 'low'
    assert calls[0][1]['top_p'] == 0.8
    assert calls[1][1]['max_new_tokens'] == 83 and calls[1][1]['reasoning_effort'] == 'high'
    assert calls[1][1]['top_p'] == 0.9
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-a')]).successful


def test_stage_setting_validation_and_inheritance(server):
    """Inherit shared defaults and reject invalid stage overrides atomically.

    A stage may override one control without changing another stage. Invalid
    ranges or reasoning values reject the entire batch, leaving prior settings.
    """
    base = Parameter('models.qwen3_4b.max_new_tokens', value=512)
    model = Parameter('models.qwen3_4b.stages.model.max_new_tokens', value=256)
    assert server.set_parameters_atomically([base, model]).successful
    assert server.stage_settings('qwen3_4b', 'model')['max_new_tokens'] == 256
    assert server.stage_settings('qwen3_4b', 'plan')['max_new_tokens'] == 512
    assert not server.set_parameters_atomically([
        Parameter('models.qwen3_4b.stages.plan.temperature', value=3.0),
        Parameter('models.qwen3_4b.stages.model.reasoning_effort', value='incorrect')]).successful
    assert server.stage_settings('qwen3_4b', 'plan')['temperature'] == 0.0


def test_acquisition_atomically_initializes_stage_settings_and_rejects_conflicts(server):
    """Initialize inherited stage values without overwriting explicit choices.

    Acquire a ready fake model while proposing two independent stage controls.
    The frozen snapshot must contain the installed values. After release, a
    conflicting explicit value must reject the entire next acquisition, leaving
    both ownership and the other proposed change untouched.
    """
    import json
    server.replace_model('qwen3_4b')
    model = 'models.qwen3_4b.stages.model.max_new_tokens'
    plan = 'models.qwen3_4b.stages.plan.max_new_tokens'
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='run-a'),
        Parameter(model, value=41), Parameter(plan, value=83)]).successful
    snapshot = json.loads(server.get_parameter('frozen_settings_json').value)
    assert snapshot['stages']['model']['max_new_tokens'] == 41
    assert snapshot['stages']['plan']['max_new_tokens'] == 83
    assert server.set_parameters_atomically([Parameter('experiment_run_id', value='release:run-a')]).successful
    assert server.set_parameters_atomically([Parameter(plan, value=-1)]).successful
    result = server.set_parameters_atomically([Parameter('experiment_run_id', value='run-b'),
        Parameter(model, value=42), Parameter(plan, value=90)])
    assert not result.successful and 'conflicts' in result.reason
    assert server.get_parameter('experiment_run_id').value == ''
    assert server.get_parameter(model).value == 41
    assert server.get_parameter(plan).value == -1


@pytest.mark.parametrize('failure', ['malformed_schema', 'invalid_schema', 'unsupported_image', 'invalid_image', 'invalid_output'])
def test_request_failure_allows_new_experiment_without_reloading(server, failure):
    """Abort a bad standalone request while preserving a usable loaded backend.

    Invalid schemas and unsupported operations fail before generation; invalid
    generated output fails after the backend returns. Neither outcome proves a
    backend failure. Require the action to abort, the exclusive slot to clear,
    and a new experiment to acquire the same loaded model without reloading it.
    Then complete an owned request and release that experiment's freeze.
    """
    from smpa_ros_interfaces.action import GenerateStructured, GenerateText, DescribeImage
    from sensor_msgs.msg import Image
    calls = []
    def generate(prompt, **settings):
        """Record a fake backend call and return invalid JSON only for schema requests."""
        calls.append((prompt, settings))
        return 'invalid JSON' if 'schema' in settings else 'fixture response'
    server.replace_model('qwen3_4b')
    server.backend.generate = generate
    backend = server.backend
    loaded = dict(server.loaded_settings)
    if failure in ('unsupported_image', 'invalid_image'):
        if failure == 'invalid_image':
            server.backend.describe = generate
        action, field = DescribeImage, 'description'
        request = FakeGoal(prompt='Describe this image', image=Image(), run_id='')
    else:
        action, field = GenerateStructured, 'json_output'
        schema = {'malformed_schema': '{', 'invalid_schema': '{"type":"made_up"}',
                  'invalid_output': '{"type":"object"}'}[failure]
        request = FakeGoal(prompt='Describe the scene', json_schema=schema, run_id='')
    assert server.reserve(request.request, inference=True) == GoalResponse.ACCEPT
    server.infer(request, action_type=action, result_field=field)
    assert request.outcome == 'aborted'
    assert not server.busy and server.state == 'ready'
    assert server.backend is backend and server.loaded_settings == loaded
    assert server.active_alias == 'qwen3_4b' and len(server.load_calls) == 1
    assert len(calls) == (1 if failure == 'invalid_output' else 0)
    acquired = server.set_parameters_atomically([Parameter('experiment_run_id', value='recovered-run')])
    assert acquired.successful, acquired.reason
    owned = FakeGoal(prompt='Continue the experiment', run_id='recovered-run')
    assert server.reserve(owned.request, inference=True) == GoalResponse.ACCEPT
    result = server.infer(owned, action_type=GenerateText, result_field='text')
    assert owned.outcome == 'succeeded' and result.text == 'fixture response'
    assert server.backend is backend and len(server.load_calls) == 1
    assert server.set_parameters_atomically([
        Parameter('experiment_run_id', value='release:recovered-run')]).successful


@pytest.mark.parametrize('error_type', [RuntimeError, ValueError])
def test_backend_failure_still_blocks_experiment_acquisition(server, error_type):
    """Retain the error state for an exception raised by the generation backend.

    A backend exception differs from a rejected request or invalid returned
    output. Abort that generation and keep the model in error state. Reject a
    fresh freeze, and ensure a later malformed request cannot erase that state.
    The fixture retains the backend so existing model management can recover it.
    """
    from smpa_ros_interfaces.action import GenerateText, GenerateStructured
    def fail(prompt, **settings):
        """Simulate a generation failure without executing any actual model."""
        raise error_type('fixture backend failure')
    server.replace_model('qwen3_4b')
    server.backend.generate = fail
    backend = server.backend
    request = FakeGoal(prompt='Generate', run_id='')
    assert server.reserve(request.request, inference=True) == GoalResponse.ACCEPT
    server.infer(request, action_type=GenerateText, result_field='text')
    assert request.outcome == 'aborted' and server.state == 'error' and not server.busy
    assert not server.set_parameters_atomically([Parameter('experiment_run_id', value='new-run')]).successful
    malformed = FakeGoal(prompt='Generate JSON', json_schema='{', run_id='')
    assert server.reserve(malformed.request, inference=True) == GoalResponse.ACCEPT
    server.infer(malformed, action_type=GenerateStructured, result_field='json_output')
    assert malformed.outcome == 'aborted' and server.state == 'error'
    assert server.backend is backend and len(server.load_calls) == 1
