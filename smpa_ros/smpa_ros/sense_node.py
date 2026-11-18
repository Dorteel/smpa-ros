"""Expose existing sensory acquisition as an independent cognitive action."""
from functools import partial
import fcntl
import hashlib
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import rclpy
from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter_client import AsyncParameterClient
import yaml

from memory import MemoryManager, locked_run, reserve_invocation
from smpa_ros.sense import capture_once, observe, record_observation
from smpa_ros_interfaces.action import DescribeImage, DescribeStructuredImage, GenerateStructured, Listen, Sense


class SenseNode(Node):
    """Acquire observations without invoking another cognitive stage."""

    def __init__(self, **node_options):
        """Connect sensing to the existing camera, speech, and inference services.

        Declare the run memory root and default operation. Create action clients
        for shared vision inference and the existing Listen service. Starting the
        node does not load model weights or start an experiment.
        """
        super().__init__('sense_stage', **node_options)
        self.declare_parameter('memory_root', str(Path.cwd() / 'memory'))
        self.declare_parameter('default_capability', 'sense')
        self.declare_parameter('inference_node', '/llm')
        self.declare_parameter('timeout', 120.0)
        self.group = ReentrantCallbackGroup()
        inference = self.get_parameter('inference_node').value.rstrip('/')
        self.image_client = ActionClient(self, DescribeImage, inference + '/describe_image', callback_group=self.group)
        self.structured_client = ActionClient(self, DescribeStructuredImage, inference + '/describe_structured_image', callback_group=self.group)
        self.structured_text = ActionClient(self, GenerateStructured, inference + '/generate_structured', callback_group=self.group)
        self.listener = ActionClient(self, Listen, '/speech/listen', callback_group=self.group)
        self.parameters = AsyncParameterClient(self, inference, callback_group=self.group)
        self.server = ActionServer(self, Sense, 'smpa/sense', self.execute,
                                   goal_callback=self.accept_goal, cancel_callback=self.cancel,
                                   callback_group=self.group)

    def accept_goal(self, request):
        """Accept the configured default or explicit Sense capability only.

        Reject unsupported capability names before allocating an invocation or
        accessing a sensor. Modality selection remains in the run configuration.
        """
        selected = request.capability or self.get_parameter('default_capability').value
        if selected != 'sense':
            self.get_logger().error(f'Unsupported Sense capability: {selected}')
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def cancel(self, goal_handle):
        """Reject cancellation until sensory and inference calls can both stop.

        The shared inference server cannot cancel an accepted backend operation.
        Never claim a completed cancellation while that operation is still active.
        """
        return CancelResponse.REJECT

    def wait(self, future, timeout_sec):
        """Wait for a bounded interval while another executor thread handles ROS.

        The caller inspects the future and decides how to report a timeout. This
        function does not claim to cancel a remote operation that is still running.
        """
        done = Event()
        def mark_completed(completed):
            """Wake the waiting callback when this ROS response completes."""
            done.set()
        future.add_done_callback(mark_completed)
        done.wait(timeout_sec)

    def result(self, future, timeout, operation):
        """Return a completed ROS response or report a named timeout.

        Wait for the response on another executor thread and propagate any error
        in the completed future. A timeout never implies remote cancellation.
        """
        self.wait(future, timeout)
        if not future.done():
            raise TimeoutError(f'Timed out waiting for {operation}')
        return future.result()

    def perform(self, client, goal):
        """Call the existing Listen action and preserve its result semantics.

        Wait for the server, acceptance and terminal response with bounded waits.
        Reject unsuccessful results instead of inventing a transcript. On a
        failure resolve late acceptance when possible, request cancellation, and
        wait briefly for the terminal result. Log an unconfirmed termination.
        """
        timeout = self.get_parameter('timeout').value
        if not client.wait_for_server(timeout_sec=timeout):
            raise TimeoutError('Listen action is unavailable')
        pending = client.send_goal_async(goal)
        handle = None
        terminal = False
        try:
            handle = self.result(pending, timeout, 'Listen acceptance')
            if not handle.accepted:
                raise RuntimeError('Listen goal was rejected')
            response_future = handle.get_result_async()
            response = self.result(response_future, timeout, 'Listen result')
            terminal = True
        finally:
            if not terminal:
                try:
                    if handle is None:
                        handle = self.result(pending, 5.0, 'late Listen acceptance')
                    if handle.accepted:
                        self.result(handle.cancel_goal_async(), 5.0, 'Listen cancellation')
                        self.result(handle.get_result_async(), 5.0, 'terminal Listen result')
                except Exception as error:
                    self.get_logger().error(f'Could not confirm Listen termination: {error}')
        if response.status != GoalStatus.STATUS_SUCCEEDED or not response.result.success:
            raise RuntimeError(response.result.error_message or 'Listen action failed')
        return response.result

    def describe(self, image, prompt, *, run_id, directory, pipeline):
        """Send one camera frame to the shared run-scoped inference server.

        The common vision function checks the frozen settings and preserves any
        refinement image before sending the request. Return its validated server
        result without loading a model in this stage process.
        """
        from smpa_ros.sense import describe_image
        return describe_image(self, image, prompt, run_id=run_id, directory=directory, pipeline=pipeline)

    def identify_questions(self, observation, *, directory, run_id, pipeline):
        """Persist the initial refinement entities and deterministic question queue.

        Send the initial observation through the shared structured text action
        using the run's frozen Sense settings. Validate unique entity identities,
        then save one question per configured property with the frozen image hash.
        No other cognitive action is invoked and no local model is loaded.
        """
        from PIL import Image as PILImage
        from smpa_ros.backends.base import stage_generation, validate_output
        from smpa_ros.plan import refinement_settings
        settings = stage_generation(pipeline, 'sense')
        if settings.get('reasoning_effort') is not None:
            raise ValueError('The shared inference server does not expose reasoning_effort')
        timeout = self.get_parameter('timeout').value
        if not self.parameters.wait_for_services(timeout_sec=timeout):
            raise TimeoutError('Inference parameter services are unavailable')
        response = self.result(self.parameters.get_parameters(['experiment_run_id', 'active_model']), timeout, 'inference ownership')
        if response.values[0].string_value != run_id or not response.values[1].string_value:
            raise RuntimeError('Inference server is not frozen for this experiment')
        alias = response.values[1].string_value
        names = [f'models.{alias}.{field}' for field in ('max_new_tokens', 'temperature', 'seed', 'top_p')]
        response = self.result(self.parameters.get_parameters(names), timeout, 'inference settings')
        actual = (response.values[0].integer_value, response.values[1].double_value,
                  response.values[2].integer_value, response.values[3].double_value)
        expected = (settings.get('max_output_tokens'), settings.get('temperature'),
                    settings.get('seed') if settings.get('seed') is not None else -1, 1.0)
        if actual != expected:
            raise ValueError(f'Inference parameters do not match frozen Sense settings: expected {expected}, got {actual}')
        schema = {'type': 'object', 'properties': {'entities': {'type': 'array', 'items': {
            'type': 'object', 'properties': {'id': {'type': 'string'}, 'description': {'type': 'string'}},
            'required': ['id', 'description'], 'additionalProperties': False}}},
            'required': ['entities'], 'additionalProperties': False}
        prompt = ('Identify distinct visible entities in this initial observation. Assign unique stable entity identifiers. '
                  'Do not invent entities. Return JSON.\n' + observation)
        if not self.structured_text.wait_for_server(timeout_sec=timeout):
            raise TimeoutError('Structured inference action is unavailable')
        goal = GenerateStructured.Goal(prompt=prompt, json_schema=json.dumps(schema), run_id=run_id)
        handle = self.result(self.structured_text.send_goal_async(goal), timeout, 'entity identification acceptance')
        if not handle.accepted:
            raise RuntimeError('Entity identification goal was rejected')
        response = self.result(handle.get_result_async(), timeout, 'entity identification result')
        if response.status != GoalStatus.STATUS_SUCCEEDED:
            raise RuntimeError('Entity identification failed')
        text = response.result.json_output
        validate_output(text, 'json_schema', schema)
        entities = json.loads(text)['entities']
        if any(not entry['id'].strip() or not entry['description'].strip() for entry in entities) or len({entry['id'] for entry in entities}) != len(entities):
            raise ValueError('Entity identification requires unique nonempty identifiers and descriptions')
        questions = [dict(target=entry['id'], property=prop,
            question=f"What is the {prop} of {entry['id']} ({entry['description']})?",
            attempted=False, status='unanswered', answer=None)
            for entry in entities for prop in refinement_settings(pipeline)['properties']]
        with PILImage.open(directory / 'observations/vision/frozen_image.png') as image:
            digest = hashlib.sha256(image.convert('RGB').tobytes()).hexdigest()
        settings = dict(settings, response_format='json_schema', output_schema=schema)
        state = dict(status='running', entities=entities, questions=questions,
                     image='observations/vision/frozen_image.png', image_sha256=digest,
                     identification=dict(prompt=prompt, generation_parameters=settings, output=text))
        memory = MemoryManager.attach_active_run(self.get_parameter('memory_root').value, run_id)
        memory.write_refinement(state)

    def execute(self, goal_handle):
        """Collect one run's configured modalities and persist their provenance.

        Resolve the existing run, reserve a Sense invocation, and use its frozen
        pipeline to collect vision, hearing and reading through existing logic.
        A run-specific lock serializes Sense writers so evidence and Read cursor
        attribution cannot overlap. Return the observation and record path on
        success; persist failures and abort without invoking another stage.
        """
        result = Sense.Result()
        record_path = None
        provenance = None
        try:
            request = goal_handle.request
            timeout = self.get_parameter('timeout').value
            if timeout <= 0:
                raise ValueError('Sense timeout must be positive')
            with locked_run(self.get_parameter('memory_root').value, request.run_id) as directory:
                pipeline = yaml.safe_load((directory / 'parameters.yaml').read_text())
                prompt = pipeline.get('stage_prompts', {}).get('sense', {}).get('name', pipeline.get('prompt', 'describe_scene'))
                cycle, name, provenance = reserve_invocation(directory, request.run_id, 'sense', prompt)
                record_path = directory / 'state' / f'cycle_{cycle:06d}' / name
            config = dict(pipeline, run_id=request.run_id, prompt=prompt)
            service = pipeline.get('_run', {}).get('service', 'vision')
            executor = SimpleNamespace(spin_until_future_complete=self.wait)
            capture = partial(capture_once, describe=partial(self.describe, run_id=request.run_id,
                                                              directory=directory, pipeline=pipeline))
            def collect(run, number):
                """Collect this invocation's enabled sensory modalities in order.

                The run directory identifies evidence ownership. Existing
                observation logic handles the selected Vision, Hear and Read
                modalities and returns the combined text without starting a stage.
                """
                return observe(self, config, run, executor, timeout, service, self, capture=capture)[0]
            from smpa_ros.plan import refinement_settings
            with (directory / '.sense.lock').open('a') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                observation, _ = record_observation(directory, cycle, collect,
                    record_name=name, provenance=provenance, prompt_name=prompt, configuration=config)
                if refinement_settings(pipeline)['enabled'] and not (directory / 'state/refinement.json').exists():
                    self.identify_questions(observation, directory=directory, run_id=request.run_id, pipeline=pipeline)
            result.observation = observation
            result.record_path = str(record_path.relative_to(directory))
            goal_handle.succeed()
        except Exception as error:
            if record_path is not None and not record_path.exists():
                try:
                    with record_path.open('x', encoding='utf-8') as output:
                        json.dump(dict(stage='sense', status='failed', error=f'{type(error).__name__}: {error}',
                                       **provenance), output, ensure_ascii=False, indent=2)
                except OSError as record_error:
                    self.get_logger().error(f'Could not persist Sense failure: {record_error}')
            self.get_logger().error(f'Sense action failed: {error}')
            goal_handle.abort()
        return result

    def destroy_node(self):
        """Destroy this stage's clients and action server before its ROS node.

        The independently owned speech and inference servers remain running and
        retain responsibility for their resources and loaded model.
        """
        self.server.destroy()
        self.listener.destroy()
        self.structured_text.destroy()
        self.image_client.destroy()
        self.structured_client.destroy()
        return super().destroy_node()


def main(args=None):
    """Run the independent Sense server until ROS shutdown.

    Two executor threads let a Sense callback wait while ROS delivers camera,
    inference and speech responses. Stop callbacks before destroying the node.
    """
    rclpy.init(args=args)
    node = SenseNode()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        executor.shutdown(wait_for_threads=True)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
