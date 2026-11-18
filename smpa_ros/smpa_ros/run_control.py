"""Adapt ROS run requests to the existing experiment in an owned child process.

Concepts:
    The control node reports lifecycle; it never chooses cognitive stage order.
    The child calls the existing smpa.run function with the existing Runner. A
    private pipe carries notifications only; persisted Runner records remain
    authoritative. Process separation keeps Runner's default ROS context and
    signals independent of the always-on control node.
"""
from functools import partial
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event, RLock, Thread

import rclpy
from ament_index_python.packages import get_package_share_directory
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from smpa_ros_interfaces.msg import SmpaEvent
from smpa_ros_interfaces.srv import GetSmpaStatus, StartSmpa, StopSmpa


def notify(stream, run_id, stage, event):
    """Write one private child notification without touching persistent records.

    Serialize only the agreed notification fields and flush them immediately.
    The control node relays these messages; all normal model and ROS logs keep
    their ordinary output streams. A closed pipe must not interrupt cognition.
    """
    try:
        stream.write(json.dumps(dict(run_id=run_id, stage=stage, event=event)) + '\n')
        stream.flush()
    except (BrokenPipeError, OSError):
        pass


def observe_stage(runner, stream, stage, operation, *args, **kwargs):
    """Notify around one existing script-requested operation without reordering it.

    Relay the operation's return value unchanged. Publish completion only after
    the existing method has persisted its record. Non-interruption failures emit
    stage_failed and are re-raised so Runner's existing context finalizes the run.
    """
    identity = runner.memory.metadata['run_id']
    notify(stream, identity, stage, 'stage_started')
    try:
        result = operation(*args, **kwargs)
    except KeyboardInterrupt:
        raise
    except BaseException:
        notify(stream, identity, stage, 'stage_failed')
        raise
    notify(stream, identity, stage, 'stage_completed')
    return result


def run_worker(descriptor, goal, runner_arguments):
    """Run the existing experiment with observation wrappers and normal cleanup.

    Construct the ordinary Runner and wrap its existing setup callback solely to
    announce the run identity after MemoryManager has persisted the new run but
    before model loading. Wrap stage methods for notifications; the unchanged
    experiment function decides when and how often they run. SIGINT follows the
    existing KeyboardInterrupt/context-manager path. Publish terminal state only
    after Runner cleanup has finalized authoritative metadata.
    """
    from runner import Runner
    from smpa_ros.smpa import run
    runner = None
    with os.fdopen(descriptor, 'w', buffering=1) as stream:
        try:
            runner = Runner(['smpa', *runner_arguments])
            setup = runner._setup

            def announce(directory, identity):
                """Announce the persisted run identity before normal setup loads weights.

                This lets StartSmpa return without waiting for model initialization
                or experiment completion. Delegate the original setup unchanged.
                """
                notify(stream, identity, '', 'run_started')
                if setup is not None:
                    return setup(directory, identity)

            runner._setup = announce
            for stage in ('sense', 'model', 'plan', 'act'):
                operation = getattr(runner, stage)
                setattr(runner, stage, partial(observe_stage, runner, stream, stage, operation))
            with runner:
                run(runner, runner.limit, goal=goal)
        except KeyboardInterrupt:
            pass
        except BaseException as error:
            print(f'SMPA worker failed: {error}', file=sys.stderr, flush=True)
        finally:
            if runner is not None and runner.memory is not None:
                identity = runner.memory.metadata['run_id']
                status = runner.memory.metadata['status']
                event = {'completed': 'run_completed', 'interrupted': 'run_interrupted'}.get(status, 'run_failed')
                notify(stream, identity, '', event)
            else:
                notify(stream, '', '', 'run_failed')


class SmpaControl(Node):
    """Expose start/stop/status while the original experiment owns execution."""

    def __init__(self, **node_options):
        """Declare run-launch parameters and begin idle without starting cognition.

        Resolve installed configuration through ament and keep all launch choices
        in ROS parameters. Freeze these parameters while a child is active. A
        private reader thread relays lifecycle events and observes child exit;
        it does not execute or reconstruct the cognitive loop.
        """
        super().__init__('smpa_control', **node_options)
        self.lock = RLock()
        self.process = None
        self.reader = None
        self.start_ready = Event()
        self.run_id = ''
        self.active_stage = ''
        self.state = 'idle'
        self.terminal_seen = False
        readonly = ParameterDescriptor(read_only=True)
        self.declare_parameter('configuration_file', str(Path(get_package_share_directory('smpa_ros')) / 'config.yaml'), readonly)
        self.declare_parameter('memory_root', str(Path.cwd() / 'memory'), readonly)
        self.declare_parameter('model_root', str(Path.cwd() / 'models'), readonly)
        self.declare_parameter('runner_arguments', ['--service', 'vision'], readonly)
        self.group = ReentrantCallbackGroup()
        self.events = self.create_publisher(SmpaEvent, 'smpa/events', 100)
        self.control_services = [
            self.create_service(StartSmpa, 'smpa/start', self.start, callback_group=self.group),
            self.create_service(StopSmpa, 'smpa/stop', self.stop, callback_group=self.group),
            self.create_service(GetSmpaStatus, 'smpa/get_status', self.status, callback_group=self.group)]

    def start(self, request, response):
        """Start one child and return its persisted identity before inference begins.

        Reject empty goals and an already active child, including startup and
        stopping. Launch the unchanged experiment with this request's goal; never
        write that goal into persistent configuration. Wait only for the identity
        notification emitted before expensive setup, or for startup failure. The
        independent reader and executor threads keep status and stop responsive.
        """
        with self.lock:
            if not request.goal.strip() or (self.state in ('running', 'stopping') or
                    self.reader is not None and self.reader.is_alive()):
                return response
            self.start_ready.clear()
            self.run_id = self.active_stage = ''
            self.state = 'running'
            self.terminal_seen = False
            read_descriptor, write_descriptor = os.pipe()
            arguments = ['--config', self.get_parameter('configuration_file').value,
                         '--world-model-root', self.get_parameter('memory_root').value,
                         '--model-root', self.get_parameter('model_root').value,
                         *self.get_parameter('runner_arguments').value]
            try:
                self.process = subprocess.Popen([sys.executable, '-m', 'smpa_ros.run_control', '--worker',
                    str(write_descriptor), request.goal, *arguments], pass_fds=(write_descriptor,),
                    start_new_session=True)
            except Exception as error:
                os.close(read_descriptor)
                self.state = 'failed'
                self.get_logger().error(str(error))
                self.events.publish(SmpaEvent(run_id='', stage='', event='run_failed'))
                return response
            finally:
                os.close(write_descriptor)
            self.reader = Thread(target=self.collect, args=(read_descriptor, self.process), daemon=True)
            self.reader.start()
        self.start_ready.wait()
        with self.lock:
            response.accepted = bool(self.run_id)
            response.run_id = self.run_id
        return response

    def collect(self, descriptor, process):
        """Relay private child notifications and detect failures without a final event.

        Update the observable run state before publishing each notification.
        Release start waiters when the real run identity is known or startup has
        failed. After pipe EOF, reap the owned child; absence of a terminal event
        becomes run_failed without inventing persisted stage results.
        """
        try:
            with os.fdopen(descriptor) as stream:
                for line in stream:
                    record = json.loads(line)
                    with self.lock:
                        self.run_id = record['run_id'] or self.run_id
                        self.active_stage = record['stage']
                        event = record['event']
                        if event.startswith('run_') and event != 'run_started':
                            self.state = {'run_completed': 'completed', 'run_interrupted': 'interrupted'}.get(event, 'failed')
                            self.terminal_seen = True
                        self.events.publish(SmpaEvent(**record))
                        if self.run_id or self.terminal_seen:
                            self.start_ready.set()
        finally:
            process.wait()
            with self.lock:
                if not self.terminal_seen:
                    interrupted = self.state == 'stopping'
                    self.state, self.active_stage = ('interrupted' if interrupted else 'failed'), ''
                    self.events.publish(SmpaEvent(run_id=self.run_id, stage='',
                        event='run_interrupted' if interrupted else 'run_failed'))
                self.start_ready.set()

    def stop(self, request, response):
        """Request the existing SIGINT cleanup path without claiming termination.

        Accept once for an active child and mark stopping before signaling its
        main process. Runner remains responsible for canceling actions, tearing
        down its participants and finalizing records. Repeated stop requests are
        rejected; an already exited child cannot be stopped retroactively.
        """
        with self.lock:
            if self.state != 'running' or self.process is None or self.process.poll() is not None:
                return response
            self.state = 'stopping'
            try:
                self.process.send_signal(signal.SIGINT)
                response.accepted = True
            except ProcessLookupError:
                response.accepted = False
        return response

    def status(self, request, response):
        """Return one consistent snapshot of lifecycle state, identity and stage.

        Notifications are advisory; this service reports the last observed child
        transition while the child's normal persisted records remain authoritative.
        """
        with self.lock:
            response.state, response.run_id, response.active_stage = self.state, self.run_id, self.active_stage
        return response

    def destroy_node(self):
        """Interrupt an owned experiment and wait for its existing cleanup to finish.

        Request SIGINT without killing unrelated processes or bypassing Runner's
        persistence. Join the notification reader before releasing ROS handles so
        late terminal notifications cannot publish through a destroyed node.
        """
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        if self.reader is not None:
            self.reader.join()
        return super().destroy_node()


def main(args=None):
    """Run either the private experiment child or the independent ROS control node.

    A child inherits no live parent ROS context because it is a fresh executable.
    Normal invocation creates only lifecycle services. On interruption drain
    service callbacks, stop the owned experiment and then release ROS resources.
    """
    arguments = list(sys.argv[1:] if args is None else args)
    if arguments and arguments[0] == '--worker':
        run_worker(int(arguments[1]), arguments[2], arguments[3:])
        return
    from rclpy.signals import SignalHandlerOptions
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = SmpaControl()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node.process is not None and node.process.poll() is None:
            node.process.send_signal(signal.SIGINT)
        executor.shutdown(wait_for_threads=True)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
