"""Expose the existing world-model generator as an independent ROS action."""
from functools import partial
import json
from pathlib import Path
from threading import Event

import rclpy
from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter_client import AsyncParameterClient
import yaml

from memory import MemoryManager, locked_run, reserve_invocation
from smpa_ros.model import generate_world_model
from smpa_ros.belief import model_messages
from smpa_ros.conversation import read_messages
from smpa_ros_interfaces.action import GenerateText, GenerateStructured, GenerateStructuredChat, Model


class ModelNode(Node):
    """Offer world-model generation without invoking other cognitive stages."""

    def __init__(self, **node_options):
        """Connect the Model action to existing memory and shared inference.

        Declare the run root, default capability, inference endpoint and timeout.
        Create clients for text, structured generation and role-preserving chat. Starting this
        node does not load a model, start a run or call another cognitive stage.
        """
        super().__init__('model_stage', **node_options)
        self.declare_parameter('memory_root', str(Path.cwd() / 'memory'))
        self.declare_parameter('default_capability', 'model')
        self.declare_parameter('inference_node', '/llm')
        self.declare_parameter('timeout', 120.0)
        self.declare_parameter('belief_ontology', '')
        self.group = ReentrantCallbackGroup()
        inference = self.get_parameter('inference_node').value.rstrip('/')
        self.text_client = ActionClient(self, GenerateText, inference + '/generate', callback_group=self.group)
        self.structured_client = ActionClient(self, GenerateStructured, inference + '/generate_structured', callback_group=self.group)
        self.chat_client = ActionClient(self, GenerateStructuredChat, inference + '/generate_structured_chat', callback_group=self.group)
        self.parameters = AsyncParameterClient(self, inference, callback_group=self.group)
        self.server = ActionServer(self, Model, 'smpa/model', self.execute,
                                   goal_callback=self.accept_goal, cancel_callback=self.cancel,
                                   callback_group=self.group)

    def accept_goal(self, request):
        """Accept the configured default, world-model or conversation-belief capability.

        An empty selection uses the configured default. Reject an unsupported
        selection before allocating a record or requesting inference.
        """
        selected = request.capability or self.get_parameter('default_capability').value
        if selected not in ('model', 'belief'):
            self.get_logger().error(f'Unsupported Model capability: {selected}')
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def cancel(self, goal_handle):
        """Reject cancellation because the shared inference backend cannot stop.

        The action must not claim cancellation while an accepted backend call
        continues and may still persist a world-model snapshot.
        """
        return CancelResponse.REJECT

    def wait(self, future, timeout, operation):
        """Wait a bounded time for a response completed by another executor thread.

        Return the future result or propagate its error. A timeout reports the
        named operation without claiming that a remote request was canceled.
        """
        done = Event()
        future.add_done_callback(lambda _: done.set())
        if not done.wait(timeout):
            raise TimeoutError(f'Timed out waiting for {operation}')
        return future.result()

    def generate(self, prompt, *, run_id, **settings):
        """Send the existing Model prompt to the shared inference action.

        Compare the active model's generation controls with the frozen run
        configuration and reject unsupported reasoning settings. Select text or
        schema-constrained generation as requested, then return successful output.
        Require the server freeze to belong to this run before comparing the
        server settings with the run configuration and sending the request.
        """
        timeout = self.get_parameter('timeout').value
        if timeout <= 0:
            raise ValueError('Model timeout must be positive')
        if settings.get('reasoning_effort') is not None:
            raise ValueError('The shared inference server does not expose reasoning_effort')
        if not self.parameters.wait_for_services(timeout_sec=timeout):
            raise TimeoutError('Inference parameter services are unavailable')
        response = self.wait(self.parameters.get_parameters(['experiment_run_id', 'active_model']), timeout, 'experiment and active model')
        if response.values[0].string_value != run_id:
            raise RuntimeError('The inference server is not frozen for this experiment')
        alias = response.values[1].string_value
        if not alias:
            raise RuntimeError('The inference server has no active model')
        names = [f'models.{alias}.{field}' for field in ('max_new_tokens', 'temperature', 'seed', 'top_p')]
        response = self.wait(self.parameters.get_parameters(names), timeout, 'inference parameters')
        actual = (response.values[0].integer_value, response.values[1].double_value,
                  response.values[2].integer_value, response.values[3].double_value)
        expected = (settings.get('max_new_tokens'), settings.get('temperature'), settings.get('seed', -1), 1.0)
        if actual != expected:
            raise ValueError(f'Inference parameters do not match frozen Model settings: expected {expected}, got {actual}')
        format_name = settings.get('response_format')
        if format_name == 'text':
            client = self.text_client
            goal = GenerateText.Goal(prompt=prompt, run_id=run_id)
        elif format_name == 'json_schema' and isinstance(settings.get('schema'), dict):
            client = self.structured_client
            goal = GenerateStructured.Goal(prompt=prompt, json_schema=json.dumps(settings['schema']), run_id=run_id)
        else:
            raise ValueError('Unsupported Model response format')
        if not client.wait_for_server(timeout_sec=timeout):
            raise TimeoutError('Inference action is unavailable')
        handle = self.wait(client.send_goal_async(goal), timeout, 'inference acceptance')
        if not handle.accepted:
            raise RuntimeError('Inference goal was rejected')
        result = self.wait(handle.get_result_async(), timeout, 'inference result')
        if result.status != GoalStatus.STATUS_SUCCEEDED:
            raise RuntimeError('Inference action failed')
        return result.result.text if format_name == 'text' else result.result.json_output

    def generate_belief(self, messages, *, run_id, snapshot, **settings):
        """Send one belief prompt with its original roles to shared inference.

        Confirm that the server still reports the exact frozen run snapshot.
        Then submit the role-preserving messages and any schema through the
        dedicated action, with the run identity checked by the server. A failed,
        rejected, or timed-out request raises instead of inventing output.
        """
        timeout = self.get_parameter('timeout').value
        if timeout <= 0 or not self.parameters.wait_for_services(timeout_sec=timeout):
            raise TimeoutError('Inference parameter services are unavailable')
        response = self.wait(self.parameters.get_parameters(['frozen_settings_json']), timeout, 'frozen inference settings')
        if not response.values[0].string_value or json.loads(response.values[0].string_value) != snapshot:
            raise RuntimeError('Inference server ownership or frozen settings changed')
        schema = settings.get('schema')
        goal = GenerateStructuredChat.Goal(messages_json=json.dumps(messages, ensure_ascii=False),
                                           json_schema=json.dumps(schema) if schema is not None else '',
                                           run_id=run_id)
        if not self.chat_client.wait_for_server(timeout_sec=timeout):
            raise TimeoutError('Chat inference action is unavailable')
        handle = self.wait(self.chat_client.send_goal_async(goal), timeout, 'chat inference acceptance')
        if not handle.accepted:
            raise RuntimeError('Chat inference goal was rejected')
        response = self.wait(handle.get_result_async(), timeout, 'chat inference result')
        if response.status != GoalStatus.STATUS_SUCCEEDED:
            raise RuntimeError('Chat inference action failed')
        return response.result.json_output

    def update_beliefs(self, request):
        """Update one active conversation run through the existing belief writer.

        Validate the supplied role-preserving history, attach to its active run,
        and require the authoritative inference snapshot recorded for that run.
        Match each role and message against the saved transcript before writing
        belief provenance, so supplied text cannot be attributed to another source.
        Use the configured ontology and the server's recorded model and generation
        settings as provenance. The existing writer owns locking, graph updates,
        durable records and failure details; report a newly recorded failure.
        """
        messages = json.loads(request.messages_json)
        if (not isinstance(messages, list) or any(not isinstance(item, dict)
                or item.get('role') not in ('user', 'assistant')
                or not isinstance(item.get('content'), str) for item in messages)):
            raise ValueError('Belief history must contain user and assistant messages')
        memory = MemoryManager.attach_active_run(self.get_parameter('memory_root').value, request.run_id)
        snapshot = memory.metadata.get('inference')
        if (not isinstance(snapshot, dict) or snapshot.get('run_id') != request.run_id
                or not isinstance(snapshot.get('loading'), dict)
                or not isinstance(snapshot.get('generation'), dict)):
            raise RuntimeError('Conversation has no recorded frozen inference settings')
        saved = read_messages(memory.directory)
        if ([{key: item[key] for key in ('role', 'content')} for item in messages] !=
                [{key: item[key] for key in ('role', 'content')} for item in saved]):
            raise ValueError('Belief history does not match the persisted conversation')
        failures = len(memory.metadata.get('belief_failed_records', []))
        model = dict(generate=partial(self.generate_belief, run_id=request.run_id, snapshot=snapshot),
                     agent=dict(snapshot['loading']), settings=dict(snapshot['generation']),
                     ontology=self.get_parameter('belief_ontology').value or None)
        model_messages(memory, model, messages)
        if len(memory.metadata.get('belief_failed_records', [])) != failures:
            raise RuntimeError('Belief update failed; see the Model run record')

    def execute(self, goal_handle):
        """Generate a world model or update conversation beliefs for one active run.

        For belief requests, delegate the history to the existing belief writer
        and return after it completes. For world-model requests, resolve the active
        run, validate the source record and previous
        snapshot as paths inside it, and reserve a durable invocation. Select
        the requested prompt name or use the configured default. Invoke the
        existing generator with frozen run settings, preserving its snapshot and
        provenance record. Return both paths on success. Record and abort errors
        without replacing a record already written by the generator.
        """
        result = Model.Result()
        record_path = None
        provenance = None
        try:
            request = goal_handle.request
            capability = getattr(request, 'capability', '') or self.get_parameter('default_capability').value
            if capability == 'belief':
                self.update_beliefs(request)
                goal_handle.succeed()
                return result
            if capability != 'model':
                raise ValueError('Unsupported Model capability')
            with locked_run(self.get_parameter('memory_root').value, request.run_id) as directory:
                for value, area in ((request.source_record, None), (request.previous_snapshot, 'world_model')):
                    if value:
                        path = (directory / value).resolve()
                        parent = directory / area if area else directory
                        if Path(value).is_absolute() or not path.is_relative_to(parent) or not path.is_file():
                            raise ValueError('Model source and previous snapshot must exist within the run')
                observations = []
                if request.source_record:
                    source = json.loads((directory / request.source_record).read_text())
                    observations = source.get('source_observations', [])
                    if not isinstance(observations, list) or any(not isinstance(item, str) for item in observations):
                        raise ValueError('Model source observations must be a list of paths')
                pipeline = yaml.safe_load((directory / 'parameters.yaml').read_text())
                prompt_name = (getattr(request, 'prompt_name', '') or
                               pipeline.get('stage_prompts', {}).get('model', {}).get('name', 'default'))
                cycle, name, provenance = reserve_invocation(directory, request.run_id, 'model', prompt_name,
                                                              request.source_record or None)
                record_path = directory / 'state' / f'cycle_{cycle:06d}' / name
            result.world_model = generate_world_model(directory, cycle, request.observation,
                                                       partial(self.generate, run_id=request.run_id),
                                                       observations=observations, pipeline=pipeline,
                                                       models=pipeline.get('_run', {}).get('models', {}),
                                                       prompt_name=prompt_name,
                                                       record_name=name, previous_snapshot=request.previous_snapshot,
                                                       provenance=provenance)
            record = json.loads(record_path.read_text())
            result.record_path = str(record_path.relative_to(directory))
            result.snapshot_path = record['iteration']
            if (directory / 'state/refinement.json').exists():
                memory = MemoryManager.attach_active_run(self.get_parameter('memory_root').value, request.run_id)
                refinement = memory.read_refinement()
                if all(item['attempted'] for item in refinement['questions']):
                    refinement['status'] = 'completed'
                    memory.write_refinement(refinement)
            goal_handle.succeed()
        except Exception as error:
            if record_path is not None and not record_path.exists():
                try:
                    with record_path.open('x', encoding='utf-8') as output:
                        json.dump(dict(stage='model', status='failed', error=f'{type(error).__name__}: {error}',
                                       **provenance), output, ensure_ascii=False, indent=2)
                except OSError as record_error:
                    self.get_logger().error(f'Could not persist Model failure: {record_error}')
            self.get_logger().error(f'Model action failed: {error}')
            goal_handle.abort()
        return result

    def destroy_node(self):
        """Release this stage's action endpoints while leaving inference running.

        Destroy the inference clients and the Model server before releasing the
        node itself. The independently owned backend is not unloaded.
        """
        self.server.destroy()
        self.text_client.destroy()
        self.structured_client.destroy()
        self.chat_client.destroy()
        return super().destroy_node()


def main(args=None):
    """Run the independent Model action server until ROS shutdown.

    Two executor threads let an action callback wait while ROS completes its
    inference and parameter requests. Stop callbacks before destroying endpoints.
    """
    rclpy.init(args=args)
    node = ModelNode()
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
