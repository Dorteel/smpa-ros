"""Expose existing action execution as an independent cognitive stage."""
from functools import partial
import fcntl
import hashlib
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter_client import AsyncParameterClient
import yaml

from memory import MemoryManager, locked_run, reserve_invocation
from smpa_ros.act import execute_action, perform_action
from smpa_ros.plan import refinement_settings
from smpa_ros_interfaces.action import Act, DescribeImage, DescribeStructuredImage


class ActNode(Node):
    """Execute a requested decision without calling another cognitive stage."""

    def __init__(self, **node_options):
        """Expose one action for decisions in already active experiment runs.

        Declare the shared memory root, default capability and timeout. The
        executor must have another thread to deliver robot action responses while
        an Act callback waits. Constructing this node never starts another stage.
        """
        super().__init__('act_stage', **node_options)
        self.declare_parameter('memory_root', str(Path.cwd() / 'memory'))
        self.declare_parameter('default_capability', 'act')
        self.declare_parameter('timeout', 120.0)
        self.declare_parameter('inference_node', '/llm')
        self.group = ReentrantCallbackGroup()
        inference = self.get_parameter('inference_node').value.rstrip('/')
        self.image_client = ActionClient(self, DescribeImage, inference + '/describe_image', callback_group=self.group)
        self.structured_client = ActionClient(self, DescribeStructuredImage, inference + '/describe_structured_image', callback_group=self.group)
        self.parameters = AsyncParameterClient(self, inference, callback_group=self.group)
        self.server = ActionServer(self, Act, 'smpa/act', self.execute,
                                   goal_callback=self.accept_goal, cancel_callback=self.cancel,
                                   callback_group=self.group)

    def accept_goal(self, request):
        """Accept the configured default or explicit Act capability.

        Reject unsupported selections before allocating a provenance record or
        sending a robot command. The decision itself is validated during execution.
        """
        selected = request.capability or self.get_parameter('default_capability').value
        if selected != 'act':
            self.get_logger().error(f'Unsupported Act capability: {selected}')
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def cancel(self, goal_handle):
        """Reject cancellation until stage-wide cancellation is implemented.

        A robot goal may continue after a timeout. Do not report cancellation of
        the cognitive action while the underlying operation can still be active.
        """
        return CancelResponse.REJECT

    def wait(self, future, timeout_sec):
        """Wait for a ROS future while another executor thread delivers responses.

        The existing action executor checks the future after this bounded wait
        and owns the timeout, cancellation and cleanup decisions.
        """
        done = Event()
        future.add_done_callback(lambda _: done.set())
        done.wait(timeout_sec)

    def result(self, future, timeout, operation):
        """Return a ROS response after a bounded wait on another executor thread.

        Propagate a completed future's exception and name any timeout. A timeout
        does not claim that the remote inference request has stopped.
        """
        self.wait(future, timeout)
        if not future.done():
            raise TimeoutError(f'Timed out waiting for {operation}')
        return future.result()

    def dispatch(self, action, record, *, pipeline, directory, run_id, cycle):
        """Execute a physical action or inspect already captured visual evidence.

        Movement uses the existing ROS action executor. Observe uses the shared
        inference server on the previously recorded image and persists its result;
        it never starts the Sense cognitive action or captures a new camera frame.
        """
        if action == 'observe':
            self.focus(record, pipeline=pipeline, directory=directory, run_id=run_id, cycle=cycle)
            return
        timeout = self.get_parameter('timeout').value
        if timeout <= 0:
            raise ValueError('Act timeout must be positive')
        executor = SimpleNamespace(spin_until_future_complete=self.wait)
        execute_action(self, executor, action, record, timeout, configuration=pipeline)

    def focus(self, record, *, pipeline, directory, run_id, cycle):
        """Answer a planned question using the latest persisted visual evidence.

        Read the run's refinement queue and reserve the requested question when
        refinement is enabled. Load the frozen image, or the latest Sense image
        for an ordinary observation, and ask the shared inference server with the
        existing focused prompt and schema. Persist the answer and its image hash,
        then update the queue and action record. A run-local lock prevents two
        focused operations from claiming the same question concurrently.
        """
        from PIL import Image as PILImage
        from sensor_msgs.msg import Image
        from smpa_ros.backends.base import stage_generation, validate_output
        from smpa_ros.sense import describe_image
        from smpa_ros.plan import refinement_settings

        parameters = record.get('parameters', {})
        target, question = parameters.get('target'), parameters['question']
        memory = MemoryManager.attach_active_run(self.get_parameter('memory_root').value, run_id)
        with (directory / '.focus.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            refinement = memory.read_refinement() if refinement_settings(pipeline)['enabled'] else None
            item = None
            if refinement is not None:
                item = next((entry for entry in refinement['questions'] if entry['target'] == target and entry['question'] == question), None)
                if item is None or item['attempted']:
                    raise ValueError('Observe requires a previously unattempted refinement question')
                item.update(attempted=True, attempted_cycle=cycle)
                memory.write_refinement(refinement)
            try:
                image_path = directory / 'observations/vision/frozen_image.png'
                if not image_path.exists():
                    records = []
                    for path in (directory / 'state').glob('cycle_*/sense*.json'):
                        saved = json.loads(path.read_text())
                        if saved.get('status') == 'generated':
                            records.append(saved)
                    records.sort(key=lambda entry: entry.get('sequence', 0), reverse=True)
                    sources = [directory / source for entry in records for source in entry.get('source_observations', [])
                               if source.endswith('.png')]
                    image_path = next((path for path in sources if path.is_file() and path.resolve().is_relative_to(directory)), None)
                    if image_path is None:
                        raise RuntimeError('Observe requires an earlier Sense camera image')
                with PILImage.open(image_path) as stored:
                    pixels = stored.convert('RGB')
                digest = hashlib.sha256(pixels.tobytes()).hexdigest()
                message = Image(height=pixels.height, width=pixels.width, encoding='rgb8',
                                is_bigendian=0, step=pixels.width * 3, data=list(pixels.tobytes()))
                prompt = pipeline.get('stage_prompts', {}).get('sense', {}).get('text', 'Describe the scene.')
                prompt += '\nFocus on: ' + (target or 'the entire image') + '\nQuestion: ' + question
                prompt += '\nUse only visible evidence. State when the answer cannot be determined visually.'
                settings = stage_generation(pipeline, 'sense')
                if item is not None:
                    prompt += ('\nReturn JSON with status answered when visible evidence answers the question, '
                               'visually_indeterminable when the property cannot be established from this image, '
                               'or unanswered when no conclusion was reached. Put the answer or explanation in answer.')
                    settings = dict(settings, response_format='json_schema', output_schema={
                        'type': 'object', 'properties': {'status': {'type': 'string',
                        'enum': ['answered', 'visually_indeterminable', 'unanswered']}, 'answer': {'type': 'string'}},
                        'required': ['status', 'answer'], 'additionalProperties': False})
                selected = dict(pipeline, stage_settings=dict(pipeline.get('stage_settings', {}), sense=settings))
                value = describe_image(self, message, prompt, run_id=run_id, directory=directory, pipeline=selected)
                validate_output(value, settings['response_format'], settings.get('output_schema') if settings['response_format'] == 'json_schema' else None)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError('Observe returned an empty observation')
                if item is not None:
                    answer = json.loads(value)
                    if answer['status'] == 'answered' and not answer['answer'].strip():
                        raise ValueError('Answered questions require a nonempty answer')
                    item.update(status=answer['status'], answer=answer['answer'])
                filename = f"observe_{cycle:06d}_{record.get('invocation', 1):03d}.json"
                path = directory / 'observations/vision' / filename
                model = pipeline.get('_run', {}).get('models', {}).get('vision', {}).get('model_id')
                observation = dict(target=target, question=question, output=value, prompt=prompt,
                                   generation_parameters=settings, image_sha256=digest, model_identifier=model)
                with path.open('x', encoding='utf-8') as output:
                    json.dump(observation, output, ensure_ascii=False, indent=2)
                source = str(path.relative_to(directory))
                record.update(goal_accepted=True, result_status=4, success=True, result=observation, observation_record=source)
                if item is not None:
                    item['source_record'] = source
            except BaseException as error:
                if item is not None:
                    item.update(status='unanswered', error=f'{type(error).__name__}: {error}')
                raise
            finally:
                if refinement is not None:
                    memory.write_refinement(refinement)

    def execute(self, goal_handle):
        """Execute one persisted decision for the requested active run.

        Validate the optional source record and reserve an invocation under the
        run lock. Use the frozen pipeline and the existing action implementation;
        preserve decision parameters and return the resulting record path. A
        refinement completion may produce a deliberate no-op. Persist failures
        and abort rather than reporting a successful or canceled operation.
        """
        result = Act.Result()
        record_path = None
        provenance = None
        try:
            request = goal_handle.request
            decision = json.loads(request.action_json)
            if decision is not None and (not isinstance(decision, dict) or not isinstance(decision.get('action'), str)):
                raise ValueError('Act requires an action decision object or null')
            with locked_run(self.get_parameter('memory_root').value, request.run_id) as directory:
                if request.source_record:
                    source = (directory / request.source_record).resolve()
                    if Path(request.source_record).is_absolute() or not source.is_relative_to(directory) or not source.is_file():
                        raise ValueError('Act source record must exist within the run')
                pipeline = yaml.safe_load((directory / 'parameters.yaml').read_text())
                prompt_name = pipeline.get('stage_prompts', {}).get('act', {}).get('name', 'default')
                cycle, name, provenance = reserve_invocation(directory, request.run_id, 'act', prompt_name,
                                                              request.source_record or None)
                record_path = directory / 'state' / f'cycle_{cycle:06d}' / name
            if decision is None:
                metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
                state_path = directory / 'state/refinement.json'
                state = json.loads(state_path.read_text()) if state_path.exists() else None
                if not refinement_settings(pipeline)['enabled'] or not (cycle >= metadata.get('max_cycles', 1) or state and state.get('status') == 'completed'):
                    raise ValueError('A null action is only allowed when refinement is complete')
                with record_path.open('x', encoding='utf-8') as output:
                    json.dump(dict(stage='act', status='completed', requested_action=None,
                                   reason='Refinement finished or at cycle limit; no execution required', **provenance), output, indent=2)
            else:
                action = decision['action']
                provenance['parameters'] = {key: value for key, value in decision.items() if key != 'action'}
                perform_action(directory, cycle, action, partial(self.dispatch, pipeline=pipeline, directory=directory, run_id=request.run_id, cycle=cycle),
                               prompt_name=prompt_name, record_name=name, provenance=provenance, configuration=pipeline)
            result.success = True
            result.record_path = str(record_path.relative_to(directory))
            goal_handle.succeed()
        except Exception as error:
            if record_path is not None and not record_path.exists():
                try:
                    with record_path.open('x', encoding='utf-8') as output:
                        json.dump(dict(stage='act', status='failed', error=f'{type(error).__name__}: {error}',
                                       **provenance), output, ensure_ascii=False, indent=2)
                except OSError as record_error:
                    self.get_logger().error(f'Could not persist Act failure: {record_error}')
            self.get_logger().error(f'Act action failed: {error}')
            goal_handle.abort()
        return result

    def destroy_node(self):
        """Close the Act server before releasing this node's ROS resources.

        Destroy this node's image-inference clients; each robot action call owns
        its own temporary client. Other stage and inference nodes remain running.
        """
        self.server.destroy()
        self.image_client.destroy()
        self.structured_client.destroy()
        return super().destroy_node()


def main(args=None):
    """Run the independent Act server until ROS shutdown.

    Keep another executor thread available for robot action responses. Stop the
    executor before destroying the node so callbacks do not use closed handles.
    """
    rclpy.init(args=args)
    node = ActNode()
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
