"""Expose the existing schema-constrained planner as an independent ROS action."""
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

from memory import locked_run, reserve_invocation
from smpa_ros.plan import refinement_settings, select_action
from smpa_ros_interfaces.action import GenerateStructured, Plan


class PlanNode(Node):
    """Offer planning without starting or selecting any other cognitive stage."""

    def __init__(self, **node_options):
        """Declare the run root and inference endpoint, then expose one action.

        The memory root identifies existing experiments; the inference node owns
        its model and generation parameters. The default capability resolves an
        empty request and must name the supported planning operation. No model is
        loaded and no experiment is started while constructing the node.
        """
        super().__init__('plan_stage', **node_options)
        self.declare_parameter('memory_root', str(Path.cwd() / 'memory'))
        self.declare_parameter('default_capability', 'plan')
        self.declare_parameter('inference_node', '/llm')
        self.declare_parameter('timeout', 120.0)
        self.group = ReentrantCallbackGroup()
        inference = self.get_parameter('inference_node').value.rstrip('/')
        self.client = ActionClient(self, GenerateStructured, inference + '/generate_structured',
                                   callback_group=self.group)
        self.parameters = AsyncParameterClient(self, inference, callback_group=self.group)
        self.server = ActionServer(self, Plan, 'smpa/plan', self.execute,
                                   goal_callback=self.accept_goal, cancel_callback=self.cancel,
                                   callback_group=self.group)

    def accept_goal(self, request):
        """Accept only the configured default or explicit planning capability.

        Empty capability selects the node parameter; `plan` requests the same
        operation explicitly. Reject other values before allocating run records.
        """
        selected = request.capability or self.get_parameter('default_capability').value
        if selected != 'plan':
            self.get_logger().error(f'Unsupported Plan capability: {selected}')
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def cancel(self, goal_handle):
        """Reject cancellation while the shared inference backend cannot stop.

        The existing inference action is blocking and rejects cancellation. Do not
        report a canceled planning operation while its model may still be running.
        """
        return CancelResponse.REJECT

    def wait(self, future, timeout, operation):
        """Wait for an executor-driven ROS response for a bounded interval.

        The future belongs to a ROS action or parameter request. A separate
        executor thread completes it while this callback waits. Propagate its
        error or return its result; report a timeout without fabricating success.
        """
        done = Event()
        future.add_done_callback(lambda _: done.set())
        if not done.wait(timeout):
            raise TimeoutError(f'Timed out waiting for {operation}')
        return future.result()

    def generate(self, prompt, *, run_id, **settings):
        """Request schema-constrained generation from the configured LLM node.

        Require the inference server to be frozen for this run. Check that the
        selected server's active model has the same generation
        controls as the frozen planning request; otherwise fail instead of
        silently recording settings that were not used. Send the actual schema
        and prompt through the existing structured-generation action. Return its
        text only on a successful ROS action result.
        """
        timeout = self.get_parameter('timeout').value
        if timeout <= 0:
            raise ValueError('Plan timeout must be positive')
        if settings.get('reasoning_effort') is not None:
            raise ValueError('The shared inference server does not expose reasoning_effort')
        schema = settings.get('schema')
        if not isinstance(schema, dict) or settings.get('response_format') != 'json_schema':
            raise ValueError('Planning requires a JSON Schema')
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
            raise ValueError(f'Inference parameters do not match frozen Plan settings: expected {expected}, got {actual}')
        if not self.client.wait_for_server(timeout_sec=timeout):
            raise TimeoutError('Structured inference action is unavailable')
        goal = GenerateStructured.Goal(prompt=prompt, json_schema=json.dumps(schema), run_id=run_id)
        handle = self.wait(self.client.send_goal_async(goal), timeout, 'inference acceptance')
        if not handle.accepted:
            raise RuntimeError('Structured inference goal was rejected')
        result = self.wait(handle.get_result_async(), timeout, 'inference result')
        if result.status != GoalStatus.STATUS_SUCCEEDED:
            raise RuntimeError('Structured inference action failed')
        return result.result.json_output

    def execute(self, goal_handle):
        """Plan once for the requested run and persist the complete decision.

        Select the requested prompt name or the configured default. Resolve the
        run ID using the configured memory root, validate an optional
        source record, and reserve one unique Plan invocation. Read the frozen run
        configuration and invoke the existing planner with the request's world
        model. Return its validated JSON and record path; log and abort failures.
        If a reserved invocation fails before the planner records it, persist its
        failure without replacing an existing record. No Sense, Model or Act
        action is invoked by this callback.
        """
        result = Plan.Result()
        record_path = None
        provenance = None
        try:
            request = goal_handle.request
            with locked_run(self.get_parameter('memory_root').value, request.run_id) as directory:
                if request.source_record:
                    source = (directory / request.source_record).resolve()
                    if Path(request.source_record).is_absolute() or not source.is_relative_to(directory) or not source.is_file():
                        raise ValueError('Plan source record must exist within the run')
                pipeline = yaml.safe_load((directory / 'parameters.yaml').read_text())
                metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
                prompt_name = (getattr(request, 'prompt_name', '') or
                               pipeline.get('stage_prompts', {}).get('plan', {}).get('name', 'default'))
                cycle, name, provenance = reserve_invocation(
                    directory, request.run_id, 'plan', prompt_name, request.source_record or None)
                record_path = directory / 'state' / f'cycle_{cycle:06d}' / name
            settings = refinement_settings(pipeline)
            state_path = directory / 'state/refinement.json'
            refinement = json.loads(state_path.read_text()) if settings['enabled'] and state_path.exists() else None
            if settings['enabled'] and (cycle >= metadata.get('max_cycles', 1) or
                                         refinement is not None and refinement.get('status') == 'completed'):
                record = dict(stage='plan', status='completed', selected_action=None,
                              reason='Refinement finished or at cycle limit; no planning required', **provenance)
                with (directory / 'state' / f'cycle_{cycle:06d}' / name).open('x', encoding='utf-8') as output:
                    json.dump(record, output, indent=2)
                result.action_json = 'null'
            else:
                if refinement is not None:
                    pending = next((item for item in refinement['questions'] if not item['attempted']), None)
                    if pending is None:
                        raise ValueError('No unattempted refinement question is available')
                    pipeline = dict(pipeline, _pending_question=dict(target=pending['target'], question=pending['question']))
                select_action(directory, cycle, request.world_model,
                              partial(self.generate, run_id=request.run_id),
                              pipeline=pipeline, prompt_name=prompt_name,
                              record_name=name, provenance=provenance)
                record = json.loads((directory / 'state' / f'cycle_{cycle:06d}' / name).read_text())
                result.action_json = record['output']
            result.record_path = str(Path('state') / f'cycle_{cycle:06d}' / name)
            goal_handle.succeed()
        except Exception as error:
            if record_path is not None and not record_path.exists():
                try:
                    with record_path.open('x', encoding='utf-8') as output:
                        json.dump(dict(stage='plan', status='failed', error=f'{type(error).__name__}: {error}',
                                       **provenance), output, ensure_ascii=False, indent=2)
                except OSError as record_error:
                    self.get_logger().error(f'Could not persist Plan failure: {record_error}')
            self.get_logger().error(f'Plan action failed: {error}')
            goal_handle.abort()
        return result

    def destroy_node(self):
        """Release this node's action endpoints without touching the LLM server.

        Stop the Plan action server and its client, then release ROS node handles.
        The inference server owns its own backend and remains running independently.
        """
        self.server.destroy()
        self.client.destroy()
        return super().destroy_node()


def main(args=None):
    """Run the independent Plan action server until ROS shutdown.

    Two executor threads let the Plan callback wait while ROS completes its
    inference and parameter requests. Stop the executor before destroying the
    node so no callback uses released action handles.
    """
    rclpy.init(args=args)
    node = PlanNode()
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
