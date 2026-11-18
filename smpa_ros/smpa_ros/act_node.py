"""Expose existing action execution as an independent cognitive stage."""
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
import yaml

from memory import locked_run, reserve_invocation
from smpa_ros.act import execute_action, perform_action
from smpa_ros.plan import refinement_settings
from smpa_ros_interfaces.action import Act


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
        self.group = ReentrantCallbackGroup()
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

    def dispatch(self, action, record, pipeline):
        """Forward a movement decision to the existing ROS action executor.

        Reuse the registered action mapping and its existing result and timeout
        handling. Focused observation requires the shared image inference path
        and is explicitly rejected until that path has been migrated.
        """
        if action == 'observe':
            raise RuntimeError('Focused observation is not yet available in the independent Act node')
        timeout = self.get_parameter('timeout').value
        if timeout <= 0:
            raise ValueError('Act timeout must be positive')
        executor = SimpleNamespace(spin_until_future_complete=self.wait)
        execute_action(self, executor, action, record, timeout, configuration=pipeline)

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
                perform_action(directory, cycle, action, lambda action, record: self.dispatch(action, record, pipeline),
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

        Robot action clients are owned and destroyed by each existing executor
        call. This cleanup does not shut down other stage or inference nodes.
        """
        self.server.destroy()
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
