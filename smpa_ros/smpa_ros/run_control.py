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

START_TIMEOUT = 30.0
SHUTDOWN_TIMEOUT = 5.0


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
        self.closing = False
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
        If no identity or failure arrives within the startup deadline, mark the
        run failed and stop the owned child without waiting indefinitely.
        """
        with self.lock:
            if not request.goal.strip() or (self.state in ('running', 'stopping') or
                    self.process is not None and self.process.poll() is None or
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
        if not self.start_ready.wait(START_TIMEOUT):
            with self.lock:
                if not self.run_id and not self.terminal_seen:
                    self.state = 'failed'
                    self.terminal_seen = True
                    self.events.publish(SmpaEvent(run_id='', stage='', event='run_failed'))
                    timed_out = True
                else:
                    timed_out = False
            if timed_out:
                self.get_logger().error('Timed out waiting for SMPA startup notification')
                self.stop_child(self.process)
        with self.lock:
            response.accepted = bool(self.run_id)
            response.run_id = self.run_id
        return response

    def collect(self, descriptor, process):
        """Relay private child notifications and detect failures without a final event.

        Update the observable run state before publishing each notification.
        Release start waiters when the real run identity is known or startup has
        failed. After pipe EOF, reap the owned child; absence of a terminal event
        becomes run_failed without inventing persisted stage results. If the
        notification pipe closes while the child is alive, stop that child within
        bounded time. Ignore late notifications after shutdown has started.
        """
        try:
            with os.fdopen(descriptor) as stream:
                for line in stream:
                    record = json.loads(line)
                    with self.lock:
                        if self.closing or self.terminal_seen:
                            continue
                        self.run_id = record['run_id'] or self.run_id
                        self.active_stage = record['stage'] if record['event'] == 'stage_started' else ''
                        event = record['event']
                        if event.startswith('run_') and event != 'run_started':
                            self.state = {'run_completed': 'completed', 'run_interrupted': 'interrupted'}.get(event, 'failed')
                            self.terminal_seen = True
                        self.events.publish(SmpaEvent(**record))
                        if self.run_id or self.terminal_seen:
                            self.start_ready.set()
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.get_logger().error(f'Invalid SMPA child notification: {error}')
        finally:
            if process.poll() is None:
                try:
                    process.wait(timeout=SHUTDOWN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    self.stop_child(process)
            with self.lock:
                if not self.terminal_seen and not self.closing:
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
        rejected; an already exited child cannot be stopped retroactively. If
        signaling fails, restore running state for a live child or mark an exited
        child failed. A monitor bounds cleanup if the child ignores SIGINT.
        """
        with self.lock:
            if self.state != 'running' or self.process is None or self.process.poll() is not None:
                return response
            self.state = 'stopping'
            try:
                self.process.send_signal(signal.SIGINT)
                response.accepted = True
            except OSError as error:
                self.state = 'running' if self.process.poll() is None else 'failed'
                self.get_logger().error(f'Could not signal SMPA child: {error}')
                return response
            Thread(target=self.monitor_stop, args=(self.process, self.reader),
                   daemon=True).start()
        return response

    def monitor_stop(self, process, reader):
        """Bound a requested stop and report failure if its outcome is unknown.

        Wait for the owned child to exit, escalating if needed, then allow the
        notification reader a bounded time to report the persisted outcome. If
        either remains active without a terminal event, mark the run failed and
        release waiters. Never invent a successful or interrupted persisted result.
        """
        self.stop_child(process, interrupt=False)
        if reader is not None:
            reader.join(SHUTDOWN_TIMEOUT)
        with self.lock:
            if (not self.closing and not self.terminal_seen and
                    (process.poll() is None or reader is not None and reader.is_alive())):
                self.state, self.active_stage = 'failed', ''
                self.terminal_seen = True
                self.events.publish(SmpaEvent(run_id=self.run_id, stage='', event='run_failed'))
                self.start_ready.set()
                self.get_logger().error('SMPA stop did not produce a terminal notification')

    def stop_child(self, process, *, interrupt=True):
        """Stop and reap the owned child using bounded escalation.

        Give Runner time to handle SIGINT and finalize its records. If it does
        not exit, request termination and then a kill, waiting only a bounded
        time at each step. Log signaling or waiting failures and never affect a
        process other than the child supplied by this control node.
        """
        if process is None or process.poll() is not None:
            return
        for operation in ((lambda: process.send_signal(signal.SIGINT)) if interrupt else None,
                          process.terminate, process.kill):
            if process.poll() is not None:
                return
            try:
                if operation is not None:
                    operation()
                process.wait(timeout=SHUTDOWN_TIMEOUT)
                return
            except subprocess.TimeoutExpired:
                continue
            except OSError as error:
                self.get_logger().error(f'Could not stop SMPA child: {error}')
        if process.poll() is None:
            self.get_logger().error('SMPA child did not exit after bounded cleanup')

    def status(self, request, response):
        """Return one consistent snapshot of lifecycle state, identity and stage.

        Notifications are advisory; this service reports the last observed child
        transition while the child's normal persisted records remain authoritative.
        """
        with self.lock:
            response.state, response.run_id, response.active_stage = self.state, self.run_id, self.active_stage
        return response

    def destroy_node(self):
        """Stop the owned child and bound the wait for its notification reader.

        Prevent late reader messages from using ROS handles during destruction.
        Give the child a chance to finalize its records before escalating. Wait
        briefly for the daemon reader and report a reader that cannot finish.
        """
        with self.lock:
            self.closing = True
        self.stop_child(self.process)
        if self.reader is not None:
            self.reader.join(SHUTDOWN_TIMEOUT)
            if self.reader.is_alive():
                self.get_logger().error('SMPA notification reader did not exit')
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
        node.stop_child(node.process)
        executor.shutdown(wait_for_threads=True)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
