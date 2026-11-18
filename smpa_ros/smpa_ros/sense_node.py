"""Expose existing sensory acquisition as an independent cognitive action."""
from functools import partial
import fcntl
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
from sensor_msgs.msg import Image
import yaml

from memory import locked_run, reserve_invocation
from smpa_ros.backends.base import stage_generation
from smpa_ros.plan import refinement_settings
from smpa_ros.sense import capture_once, observe, record_observation
from smpa_llm.inference.images import image_to_pil
from smpa_ros_interfaces.action import DescribeImage, DescribeStructuredImage, Listen, Sense


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
        timeout request cancellation if the accepted goal handle is available.
        """
        timeout = self.get_parameter('timeout').value
        if not client.wait_for_server(timeout_sec=timeout):
            raise TimeoutError('Listen action is unavailable')
        handle = self.result(client.send_goal_async(goal), timeout, 'Listen acceptance')
        if not handle.accepted:
            raise RuntimeError('Listen goal was rejected')
        try:
            response = self.result(handle.get_result_async(), timeout, 'Listen result')
        except Exception:
            handle.cancel_goal_async()
            raise
        if response.status != GoalStatus.STATUS_SUCCEEDED or not response.result.success:
            raise RuntimeError(response.result.error_message or 'Listen action failed')
        return response.result

    def describe(self, image, prompt, *, run_id, directory, pipeline):
        """Describe camera pixels with the run-owned shared inference server.

        Check the active experiment and generation controls against the frozen
        Sense configuration. Preserve the original image for refinement and send
        a bounded RGB copy with the request-specific schema when configured.
        Return only successful inference output. Never load a model locally.
        """
        timeout = self.get_parameter('timeout').value
        settings = stage_generation(pipeline, 'sense')
        if settings.get('reasoning_effort') is not None:
            raise ValueError('The shared inference server does not expose reasoning_effort')
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
        pixels = image_to_pil(image)
        if refinement_settings(pipeline)['enabled']:
            frozen = directory / 'observations/vision/frozen_image.png'
            if frozen.exists():
                from PIL import Image as PILImage
                with PILImage.open(frozen) as stored:
                    pixels = stored.convert('RGB')
            else:
                with frozen.open('xb') as output:
                    pixels.save(output, format='PNG')
        pixels.thumbnail((512, 512))
        message = Image(height=pixels.height, width=pixels.width, encoding='rgb8',
                        is_bigendian=0, step=pixels.width * 3, data=list(pixels.tobytes()))
        format_name = settings.get('response_format')
        if format_name == 'text':
            client = self.image_client
            goal = DescribeImage.Goal(image=message, prompt=prompt, run_id=run_id)
        elif format_name == 'json_schema' and isinstance(settings.get('output_schema'), dict):
            client = self.structured_client
            goal = DescribeStructuredImage.Goal(image=message, prompt=prompt,
                                                  json_schema=json.dumps(settings['output_schema']), run_id=run_id)
        else:
            raise ValueError('Unsupported Sense response format')
        if not client.wait_for_server(timeout_sec=timeout):
            raise TimeoutError('Vision inference action is unavailable')
        handle = self.result(client.send_goal_async(goal), timeout, 'vision acceptance')
        if not handle.accepted:
            raise RuntimeError('Vision inference goal was rejected')
        response = self.result(handle.get_result_async(), timeout, 'vision result')
        if response.status != GoalStatus.STATUS_SUCCEEDED:
            raise RuntimeError('Vision inference action failed')
        return response.result.description if format_name == 'text' else response.result.json_output

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
            with (directory / '.sense.lock').open('a') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                observation, _ = record_observation(directory, cycle, collect,
                    record_name=name, provenance=provenance, prompt_name=prompt, configuration=config)
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
