"""Expose registered inference models without acquiring cognitive responsibilities.

Concepts:
    A registered alias selects an existing catalogue entry or the configured shared
    primary model. A loaded configuration is the immutable loading snapshot for the
    sole backend. Parameter edits stage future loading settings; only an explicit
    management action replaces that backend. Generation settings are snapshotted
    per request. An operation reservation rejects overlapping exclusive actions.
"""
from copy import deepcopy
from functools import partial
import gc
import json
from pathlib import Path
import sys
from threading import RLock

import rclpy
from ament_index_python.packages import get_package_share_directory
from rcl_interfaces.msg import ParameterDescriptor, SetParametersResult
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
import yaml

from smpa_ros_interfaces.action import (DescribeImage, DescribeStructuredImage, GenerateStructured, GenerateText,
                                        LoadModel, SetActiveModel, UnloadModel)
from smpa_ros_interfaces.msg import InferenceStatus
from smpa_ros_interfaces.srv import GetModelStatus, ListModels
from smpa_llm.inference.backends.base import validate_generation, validate_output

LOADING_FIELDS = ('model_id', 'path', 'backend', 'device', 'dtype', 'enable_thinking')
GENERATION_FIELDS = ('max_new_tokens', 'temperature', 'top_p', 'seed')


def registered_models(configuration, registry):
    """Read catalogue entries and overlay the already selected shared primary model.

    Preserve all catalogue aliases, including specialized engines that this node
    cannot serve. Add or overlay the primary configuration's alias so its existing
    path and device defaults win over catalogue values. Do not load or download
    anything; return independent mappings for later ROS parameter declaration.
    """
    entries = deepcopy(yaml.safe_load(Path(registry).read_text())['models'])
    primary = configuration['model']
    alias = primary.get('model', primary['model_id'])
    entries[alias] = dict(entries.get(alias, {}), **primary)
    return entries


def load_registered_backend(settings, model_root, configuration_file):
    """Instantiate one existing backend using validated registered loading settings.

    Resolve local paths against the configured weight root, then reuse the existing
    asset verifier to distinguish vision from causal checkpoints. Delegate weight
    loading to the existing constructors; never copy their loading implementation.
    Causal loading currently supports its constructor's automatic device/dtype only.
    API entries reuse existing provider configuration. Specialized engines and remote
    Ollama lifetime management are outside this node's verified ownership contract.
    No missing asset is downloaded and an unsupported setting fails explicitly.
    """
    backend = settings['backend']
    if backend == 'api':
        from smpa_llm.inference.backends.api import APIBackend
        instance = APIBackend(configuration_file, provider=settings.get('provider', 'openai'),
                              modality=settings.get('modality', 'text'))
        instance.model_id = settings['model_id']
        return instance
    if backend != 'transformers':
        raise ValueError(f'This server cannot own model lifetime for backend {backend}')
    from smpa_llm.models.loading import local_model_info
    from smpa_llm.inference.backends.transformers import TransformersBackend, TransformersVLM
    root = Path(model_root).expanduser().resolve()
    path = Path(settings['path']).expanduser()
    if not path.is_absolute():
        path = (root.parent if path.parts and path.parts[0] == root.name else root) / path
    vision = local_model_info(path, vision=True, model_id=settings['model_id'], model_root=root)
    if vision['status'] == 'complete':
        if settings['enable_thinking']:
            raise ValueError('The existing image backend does not expose thinking configuration')
        return TransformersVLM(str(path), settings['dtype'], device=settings['device'])
    text = local_model_info(path, model_id=settings['model_id'], model_root=root)
    if text['status'] != 'complete':
        raise ValueError(f"Local model unavailable: {text['reason']}; vision: {vision['reason']}")
    if settings['device'] != 'auto' or settings['dtype'] != 'auto':
        raise ValueError('The existing causal loader supports device=auto and dtype=auto only')
    return TransformersBackend(str(path), enable_thinking=settings['enable_thinking'])


class InferenceServer(Node):
    """Own one backend and serialize management and inference through ROS actions."""

    def __init__(self, *, loader=None, **node_options):
        """Declare registered model namespaces and expose management without autoloading.

        Resolve package resources, then declare typed parameters with ROS overrides
        taking precedence over the shared primary configuration and catalogue.
        Validate all values before opening endpoints. The optional loader permits
        tests to exercise lifetime rules without weights. Start unloaded unless an
        explicit initial active_model was supplied. Use a reentrant callback group
        so short status services remain responsive during an exclusive action.
        """
        super().__init__('llm', **node_options)
        share = Path(get_package_share_directory('smpa_llm'))
        readonly = ParameterDescriptor(read_only=True)
        self.declare_parameter('configuration_file', str(share / 'config.yaml'), readonly)
        self.declare_parameter('registry_file', str(share / 'models/model_library.yaml'), readonly)
        self.declare_parameter('model_root', str(Path.cwd() / 'models'), readonly)
        self.configuration_file = self.get_parameter('configuration_file').value
        self.model_root = self.get_parameter('model_root').value
        from smpa_llm.configuration import load_configuration
        self.configuration = load_configuration(self.configuration_file)
        self.catalogue = registered_models(self.configuration,
                                          self.get_parameter('registry_file').value)
        self.lock = RLock()
        self.busy = False
        self.backend = None
        self.loaded_settings = None
        self.active_alias = ''
        self.state = 'unloaded'
        self.internal_parameter_update = False
        self.loader = loader or load_registered_backend
        self.declare_parameter('active_model', '')
        self.declare_parameter('experiment_run_id', '')
        if self.get_parameter('experiment_run_id').value:
            raise ValueError('experiment_run_id can only be acquired after the model is ready')
        initial = self.get_parameter('active_model').value
        for alias, entry in self.catalogue.items():
            defaults = dict(model_id=entry.get('model_id', entry.get('base_model', {}).get('model_id', alias)), path=entry.get('path', ''),
                            backend=entry['backend'], device=entry.get('device', 'auto'),
                            dtype=entry.get('dtype', 'auto'),
                            enable_thinking=entry.get('enable_thinking', False),
                            max_new_tokens=128, temperature=0.0, top_p=1.0, seed=-1)
            for field, value in defaults.items():
                self.declare_parameter(f'models.{alias}.{field}', value)
            self.validate_settings(self.settings(alias))
        self.add_on_set_parameters_callback(self.validate_parameters)
        self.group = ReentrantCallbackGroup()
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                         reliability=ReliabilityPolicy.RELIABLE)
        self.status_publisher = self.create_publisher(InferenceStatus, 'llm/inference_status', qos)
        self.management_services = [
            self.create_service(ListModels, 'llm/list_models', self.list_models, callback_group=self.group),
            self.create_service(GetModelStatus, 'llm/get_model_status', self.get_status, callback_group=self.group)]
        self.actions = []
        for action_type, name in [(LoadModel, 'load_model'), (UnloadModel, 'unload_model'),
                                  (SetActiveModel, 'set_active_model')]:
            self.actions.append(ActionServer(self, action_type, 'llm/' + name,
                partial(self.manage, action_type=action_type, operation=name),
                goal_callback=partial(self.reserve, management=True), cancel_callback=self.cancel,
                callback_group=self.group))
        for action_type, name, field in [(GenerateText, 'generate', 'text'),
                (GenerateStructured, 'generate_structured', 'json_output'),
                (DescribeImage, 'describe_image', 'description'),
                (DescribeStructuredImage, 'describe_structured_image', 'json_output')]:
            self.actions.append(ActionServer(self, action_type, 'llm/' + name,
                partial(self.infer, action_type=action_type, result_field=field),
                goal_callback=partial(self.reserve, inference=True), cancel_callback=self.cancel,
                callback_group=self.group))
        self.change_state('unloaded')
        if initial:
            try:
                self.replace_model(initial)
            except Exception as error:
                self.get_logger().error(str(error))
                self.set_loaded_alias('')
                self.change_state('error')

    def set_parameters(self, parameters):
        """Keep each regular ROS parameter update coherent with request snapshots.

        Hold the server lock through ROS validation and value installation, not
        just its validation callback. During Node construction the lock may not
        exist yet; no concurrent request can run at that time.
        """
        lock = getattr(self, 'lock', None)
        if lock is None:
            return super().set_parameters(parameters)
        with lock:
            return [self.set_parameters_atomically([parameter]) for parameter in parameters]

    def set_parameters_atomically(self, parameters):
        """Keep atomic batches indivisible to inference and model-loading snapshots.

        Serialize both validation and ROS value installation under the same lock
        used by snapshots. An active experiment can be released only with
        `release:<run_id>`, checked against its current owner under this lock.
        Normalize a matching command to the empty parameter value, and reject a
        bare or stale release. A rejected batch installs no partial configuration.
        """
        lock = getattr(self, 'lock', None)
        if lock is None:
            return super().set_parameters_atomically(parameters)
        with lock:
            from rclpy.parameter import Parameter
            owner = self.get_parameter('experiment_run_id').value
            if sum(parameter.name == 'experiment_run_id' for parameter in parameters) > 1:
                return SetParametersResult(successful=False, reason='Experiment ownership may appear only once in a batch')
            normalized = []
            for parameter in parameters:
                if parameter.name == 'experiment_run_id' and isinstance(parameter.value, str):
                    value = parameter.value
                    if value.startswith('release:'):
                        if not owner or value[8:] != owner:
                            return SetParametersResult(successful=False, reason='Release does not match the active experiment')
                        parameter = Parameter('experiment_run_id', value='')
                    elif not value and owner:
                        return SetParametersResult(successful=False, reason='Release must name the active experiment')
                normalized.append(parameter)
            return super().set_parameters_atomically(normalized)

    def settings(self, alias):
        """Return a fresh namespace snapshot for one registered alias.

        Copy immutable catalogue metadata and replace the declared loading and
        generation fields with current ROS values. Unknown aliases fail before
        any model is released or inference is attempted. Call under the node lock
        when consistency with a concurrent parameter update matters.
        """
        if alias not in self.catalogue:
            raise ValueError(f'Unknown registered model: {alias}')
        values = dict(self.catalogue[alias])
        values.update({field: self.get_parameter(f'models.{alias}.{field}').value
                       for field in (*LOADING_FIELDS, *GENERATION_FIELDS)})
        return values

    def validate_settings(self, values):
        """Reject invalid typed controls without loading or changing a backend.

        Strings identify loading choices; device and precision use the existing
        loader's vocabulary. Seed -1 means unset, otherwise use a nonnegative
        signed 32-bit value. Validate numeric sampling ranges and the existing
        generation budget contract. Backend-specific unsupported controls also
        fail at the operation that would use them rather than being ignored.
        """
        for name in ('model_id', 'path', 'backend', 'device', 'dtype'):
            if type(values[name]) is not str:
                raise ValueError(f'{name} must be a string')
        if not values['model_id'] or not values['backend']:
            raise ValueError('model_id and backend must be nonempty')
        if type(values['enable_thinking']) is not bool:
            raise ValueError('enable_thinking must be boolean')
        if values['device'] not in ('auto', 'cpu', 'cuda'):
            raise ValueError('device must be auto, cpu, or cuda')
        if values['dtype'] not in ('auto', 'float32', 'float16', 'bfloat16'):
            raise ValueError('Unsupported dtype')
        import math
        if type(values['top_p']) is not float or not math.isfinite(values['top_p']) or not 0 < values['top_p'] <= 1:
            raise ValueError('top_p must be a finite float in (0, 1]')
        if type(values['temperature']) is not float:
            raise ValueError('temperature must be a float')
        if type(values['seed']) is not int or not -1 <= values['seed'] < 2**31:
            raise ValueError('seed must be -1 (unset) or a nonnegative signed 32-bit integer')
        validate_generation(dict(max_output_tokens=values['max_new_tokens'],
                                 temperature=values['temperature'],
                                 seed=None if values['seed'] == -1 else values['seed']))

    def validate_parameters(self, parameters):
        """Validate an atomic proposed update while keeping loading edits staged.

        Inspect each affected registered namespace after applying all proposed
        values to its snapshot. Direct active-model changes are rejected because
        only successful management may advertise readiness for a different model.
        Loading edits do not mutate the live backend. A run ID freezes the active
        model and all model settings after an idle, ready server is acquired.
        Reject unknown parameters and changes that would invalidate the freeze;
        ordinary ROS use_sim_time remains available. Return an actionable reason
        instead of partially applying an invalid batch.
        """
        with self.lock:
            changed = {}
            try:
                frozen = self.get_parameter('experiment_run_id').value
                transition = next((p.value for p in parameters if p.name == 'experiment_run_id'), frozen)
                if type(transition) is not str:
                    raise ValueError('experiment_run_id must be a string')
                if frozen and transition not in ('', frozen):
                    raise ValueError('An experiment already owns the inference server')
                if transition != frozen:
                    if self.busy:
                        raise ValueError('Cannot change experiment ownership during an operation')
                    if transition and (self.backend is None or self.state != 'ready'):
                        raise ValueError('An experiment requires a ready model')
                    if transition and any(self.settings(self.active_alias)[key] != self.loaded_settings[key]
                                          for key in LOADING_FIELDS):
                        raise ValueError('Reload staged model settings before starting an experiment')
                    if any(p.name.startswith('models.') or p.name == 'active_model' for p in parameters):
                        raise ValueError('Change model settings before acquiring or releasing an experiment')
                for parameter in parameters:
                    if parameter.name == 'experiment_run_id':
                        continue
                    if frozen and (parameter.name.startswith('models.') or parameter.name == 'active_model'):
                        raise ValueError('Inference settings are frozen for an active experiment')
                    if parameter.name == 'active_model':
                        if not self.internal_parameter_update and parameter.value != self.active_alias:
                            raise ValueError('Use load_model or set_active_model to select a model')
                    elif parameter.name == 'use_sim_time':
                        continue
                    else:
                        prefix, field = parameter.name.rsplit('.', 1)
                        alias = prefix.removeprefix('models.')
                        if prefix != 'models.' + alias or field not in (*LOADING_FIELDS, *GENERATION_FIELDS):
                            raise ValueError('Unknown inference parameter')
                        changed.setdefault(alias, self.settings(alias))[field] = parameter.value
                for values in changed.values():
                    self.validate_settings(values)
            except (ValueError, KeyError, TypeError) as error:
                return SetParametersResult(successful=False, reason=str(error))
        return SetParametersResult(successful=True)

    def change_state(self, state):
        """Publish the actual state and loaded alias after a state transition.

        Store readiness under the node lock and publish a retained reliable sample.
        The alias names the currently loaded backend, or is empty after unloading;
        staged settings never appear as a successfully loaded replacement.
        """
        with self.lock:
            self.state = state
            self.status_publisher.publish(InferenceStatus(state=state, active_model=self.active_alias))

    def set_loaded_alias(self, alias):
        """Synchronize the read-visible active parameter with successful management.

        Permit only this internal atomic update to select or clear the alias.
        Reset the permission even if validation unexpectedly fails, so a later
        external parameter request cannot bypass the management contract.
        """
        from rclpy.parameter import Parameter
        with self.lock:
            self.internal_parameter_update = True
            try:
                result = self.set_parameters_atomically([Parameter('active_model', value=alias)])
                if not result.successful:
                    raise RuntimeError(result.reason)
                self.active_alias = alias
            finally:
                self.internal_parameter_update = False

    def release_model(self):
        """Release the idle backend and its owned client or local weight references.

        Exclusive operation ownership ensures no inference uses this object.
        Close an API client when present, clear every server reference, collect
        Python objects and release unused CUDA cache allocations when Torch is
        already imported. Do not terminate or unload somebody else's API server.
        """
        self.change_state('unloading')
        backend = self.backend
        self.backend = None
        self.loaded_settings = None
        self.set_loaded_alias('')
        try:
            client = getattr(backend, '_client', None)
            if client is not None:
                client.close()
        finally:
            del backend
            gc.collect()
            torch = sys.modules.get('torch')
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
        self.change_state('unloaded')

    def replace_model(self, alias):
        """Make one registered model ready without holding two loaded instances.

        Snapshot staged loading settings before releasing the old backend. An
        identical loaded alias and snapshot returns ready without another load.
        Otherwise release the old model first, then load the replacement. Failure
        leaves no backend and a cleared active alias in recoverable error state;
        another load action may retry. Generation edits never force a reload.
        """
        with self.lock:
            values = self.settings(alias)
            loading = {key: values[key] for key in LOADING_FIELDS}
            loading.update({key: values[key] for key in ('provider', 'modality') if key in values})
            if self.backend is not None and alias == self.active_alias and loading == self.loaded_settings:
                self.change_state('ready')
                return
        self.release_model()
        self.change_state('loading')
        try:
            backend = self.loader(values, self.model_root, self.configuration_file)
            with self.lock:
                self.backend = backend
                self.loaded_settings = loading
                self.set_loaded_alias(alias)
            self.change_state('ready')
        except Exception:
            self.change_state('error')
            raise

    def reserve(self, goal, *, management=False, inference=False):
        """Reserve the sole operation slot or promptly reject an overlapping goal.

        Keep status and parameter services responsive while exactly one management
        or inference action owns backend access. Reject model management while
        an experiment owns the loaded settings. For an inference goal carrying a
        run ID, require the same active experiment while holding the reservation
        lock. Empty IDs retain standalone inference only when no run owns the
        server. The slot is released in execute
        cleanup, including failure. No second goal is queued against stale model
        settings or allowed to unload weights used by an active generation.
        """
        with self.lock:
            owner = self.get_parameter('experiment_run_id').value
            if self.busy or (management and owner):
                return GoalResponse.REJECT
            requested_run = goal.run_id if inference else ''
            if inference and requested_run != owner:
                return GoalResponse.REJECT
            self.busy = True
            return GoalResponse.ACCEPT

    def cancel(self, goal_handle):
        """Reject cancellation that the existing blocking backends cannot honor.

        These backends expose neither cooperative stopping nor incremental output.
        Keep an accepted operation running to its real completion rather than
        marking it canceled while it still uses a model. Management is likewise
        noninterruptible. No fake feedback or simulated cancellation is emitted.
        """
        return CancelResponse.REJECT

    def manage(self, goal_handle, *, action_type, operation):
        """Execute one accepted model action and report its actual outcome.

        Publish minimal operation feedback, then release or replace the idle
        backend. Mark success only after readiness or complete unloading. Log
        failure and return the error through the management result. Always release
        the reservation so a failed load can be retried by a later action.
        """
        result = action_type.Result()
        try:
            goal_handle.publish_feedback(action_type.Feedback(stage=operation))
            if operation == 'unload_model':
                self.release_model()
            else:
                self.replace_model(goal_handle.request.model_id)
            result.success = True
            goal_handle.succeed()
        except Exception as error:
            result.error_message = str(error)
            self.change_state('error')
            self.get_logger().error(str(error))
            goal_handle.abort()
        finally:
            with self.lock:
                self.busy = False
        return result

    def infer(self, goal_handle, *, action_type, result_field):
        """Generate through the existing backend using one parameter snapshot.

        Require a ready backend, then capture generation controls and the immutable
        loaded identity under the state lock. Require a callable for the requested
        operation before calling the backend. Validate a structured request's schema
        before passing it to the backend's constrained generation path. Structured
        image requests forward that schema to the existing vision backend. Decode images
        with the same converter used by legacy Sense. Never synthesize feedback.
        Existing blocking backends reject cancellation; successful completion uses
        the native success state and failures abort with a logged diagnostic because
        the agreed minimal inference results contain no error field. Keep weights
        loaded after a request failure and release the exclusive slot in all cases.
        """
        result = action_type.Result()
        try:
            with self.lock:
                if self.backend is None:
                    raise RuntimeError('No active model; use load_model first')
                values = self.settings(self.active_alias)
                backend_kind = self.loaded_settings['backend']
            request = goal_handle.request
            if not request.prompt.strip():
                raise ValueError('prompt must be nonempty')
            is_image = action_type in (DescribeImage, DescribeStructuredImage)
            if not is_image and not callable(getattr(self.backend, 'generate', None)):
                raise ValueError('The active model does not support text generation')
            schema = json.loads(request.json_schema) if result_field == 'json_output' else None
            if result_field == 'json_output':
                validate_generation(dict(response_format='json_schema', output_schema=schema))
                if backend_kind not in ('transformers', 'api'):
                    raise ValueError('Backend cannot enforce the requested schema')
            options = dict(max_new_tokens=values['max_new_tokens'], temperature=values['temperature'])
            if values['seed'] != -1:
                options['seed'] = values['seed']
            if schema is not None:
                options.update(schema=schema, response_format='json_schema')
            self.change_state('generating')
            if is_image:
                if not callable(getattr(self.backend, 'describe', None)) or (
                        backend_kind == 'api' and getattr(self.backend, 'modality', '') != 'vision'):
                    raise ValueError('The active model does not support images')
                if values['top_p'] != 1.0:
                    raise ValueError('The existing image backend does not expose top_p')
                from smpa_llm.inference.images import image_to_pil
                output = self.backend.describe(image_to_pil(request.image), request.prompt, **options)
            elif values['top_p'] != 1.0:
                if backend_kind == 'transformers':
                    controls = dict(temperature=values['temperature'], top_p=values['top_p'],
                                    do_sample=values['temperature'] > 0)
                    if values['seed'] != -1:
                        controls['seed'] = values['seed']
                    output = self.backend.generate(request.prompt, **options,
                                                   _conversation_settings=controls)
                elif backend_kind == 'api':
                    output = self.backend.generate_conversation(request.prompt,
                        max_new_tokens=values['max_new_tokens'], schema=schema,
                        temperature=values['temperature'], top_p=values['top_p'],
                        **({'seed': values['seed']} if values['seed'] != -1 else {}))['response']
                else:
                    raise ValueError('Backend does not expose top_p')
            else:
                output = self.backend.generate(request.prompt, **options)
            output = validate_output(output, 'json_schema' if schema is not None else 'text', schema)
            setattr(result, result_field, output)
            goal_handle.succeed()
            self.change_state('ready')
        except Exception as error:
            self.get_logger().error(str(error))
            goal_handle.abort()
            self.change_state('error')
        finally:
            with self.lock:
                self.busy = False
        return result

    def list_models(self, request, response):
        """List registered aliases without checking assets or loading models.

        Return aliases whose current configuration uses a supported adapter and
        supplies its essential local path or API provider and modality. Exclude
        specialized engines and incomplete selections. Local assets, credentials,
        and remote provider availability are checked only when loading or using
        a model, so listing never loads weights or contacts a provider.
        """
        with self.lock:
            response.model_ids = []
            for alias in self.catalogue:
                values = self.settings(alias)
                if values['backend'] == 'transformers' and values['path'].strip():
                    response.model_ids.append(alias)
                elif values['backend'] == 'api':
                    provider = self.configuration
                    if 'base_url' not in provider:
                        provider = provider.get(values.get('provider', 'openai'), {})
                    modality = values.get('modality', 'text')
                    if (isinstance(provider, dict) and modality in ('text', 'vision')
                            and provider.get('base_url') and provider.get('api_key_env')
                            and isinstance(provider.get('models'), dict)
                            and provider['models'].get(modality)):
                        response.model_ids.append(alias)
        return response

    def get_status(self, request, response):
        """Return a consistent readiness and loaded-alias snapshot.

        Acquire the short state lock only; inference and blocking model loading
        do not hold it, so this service can report activity while work continues.
        """
        with self.lock:
            response.state, response.active_model = self.state, self.active_alias
        return response

    def destroy_node(self):
        """Release idle model resources after the executor has drained callbacks.

        Callers must stop and join the executor before destruction. Release an
        existing experiment freeze using its owner-checked command, then unload
        the sole backend, destroy action servers and release ROS node handles.
        Reject cleanup if an operation is still active rather than unloading its
        backend. Restarting the server always starts without an experiment owner.
        """
        from rclpy.parameter import Parameter
        owner = self.get_parameter('experiment_run_id').value
        if owner:
            result = self.set_parameters_atomically([Parameter('experiment_run_id', value='release:' + owner)])
            if not result.successful:
                raise RuntimeError(f'Cannot release experiment during shutdown: {result.reason}')
        if self.backend is not None:
            self.release_model()
        for action in self.actions:
            action.destroy()
        return super().destroy_node()


def main(args=None):
    """Spin the independent inference adapter and drain operations before cleanup.

    Initialize ROS, construct one server and use multiple executor threads so
    discovery, parameters and status remain responsive during blocking actions.
    On interruption, drain active work before releasing the backend and context.
    Legacy Runner execution does not import or require this executable.
    """
    from rclpy.signals import SignalHandlerOptions
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = None
    executor = MultiThreadedExecutor(num_threads=4)
    try:
        node = InferenceServer()
        executor.add_node(node)
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        executor.shutdown(wait_for_threads=True)
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
