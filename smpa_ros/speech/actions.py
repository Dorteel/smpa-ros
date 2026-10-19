"""Two action servers sharing an exclusive, per-user audio lease on Ubuntu."""
import fcntl
import math
import os
from pathlib import Path
import tempfile
from threading import Event, Lock
from time import monotonic, sleep

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from smpa_ros_interfaces.action import Listen, Talk
import yaml

from smpa_ros.backends.speech import FasterWhisperBackend, PiperBackend


class SpeechServer(Node):
    def __init__(self, kind, backend=None, lock_path=None):
        super().__init__('listener' if kind == 'listen' else 'talker')
        self.kind = kind
        self.action_type = Listen if kind == 'listen' else Talk
        self.declare_parameter('model', 'faster-whisper-base' if kind == 'listen' else 'piper-lessac-medium')
        self.declare_parameter('model_root', str(Path.cwd() / 'models'))
        self.declare_parameter('audio_device', '')
        self.declare_parameter('max_duration', 30.0)
        self.declare_parameter('silence_seconds', 0.8)
        self.declare_parameter('speech_threshold', 0.015)
        self.declare_parameter('audio_lock', str(Path(tempfile.gettempdir()) / f'smpa-speech-{os.getuid()}.lock'))
        self.lock_path = Path(lock_path or self.get_parameter('audio_lock').value)
        self.guard = Lock()
        self.lease = None
        self.cancel = Event()
        self.stopping = False
        if backend is None:
            registry = Path(get_package_share_directory('smpa_ros')) / 'models/model_library.yaml'
            model = yaml.safe_load(registry.read_text())['models'][self.get_parameter('model').value]
            path = Path(self.get_parameter('model_root').value) / model['path']
            device = self.get_parameter('audio_device').value
            device = int(device) if device.isdigit() else device or None
            self.get_logger().info(f"Loading {model['backend']} once on CPU: {path}")
            if kind == 'listen':
                backend = FasterWhisperBackend(path, device, self.get_parameter('silence_seconds').value,
                                               self.get_parameter('speech_threshold').value)
            else:
                backend = PiperBackend(path, device)
        self.backend = backend
        self.server = ActionServer(self, self.action_type, f'/speech/{kind}', self.execute,
                                   goal_callback=self.accept, cancel_callback=self.cancel_goal,
                                   callback_group=ReentrantCallbackGroup())
        self.get_logger().info(f'/speech/{kind} action ready')

    def accept(self, goal):
        if self.kind == 'listen':
            valid = (math.isfinite(goal.max_duration) and 0 < goal.max_duration
                     <= self.get_parameter('max_duration').value)
        else:
            valid = bool(goal.text.strip())
        with self.guard:
            if not valid or self.stopping or self.lease is not None:
                return GoalResponse.REJECT
            lease = self.lock_path.open('a')
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lease.close()
                self.get_logger().warning('Audio is busy; rejecting overlapping speech goal')
                return GoalResponse.REJECT
            self.lease = lease
            self.cancel = Event()
        return GoalResponse.ACCEPT

    def cancel_goal(self, goal):
        self.cancel.set()
        self.backend.stop()
        return CancelResponse.ACCEPT

    def feedback(self, goal, state, value):
        if self.kind == 'listen':
            message = Listen.Feedback(state=state, elapsed_seconds=float(value))
        else:
            message = Talk.Feedback(state=state, progress=float(value))
        goal.publish_feedback(message)

    def execute(self, goal):
        result = self.action_type.Result()
        error = None
        try:
            report = lambda state, value: self.feedback(goal, state, value)
            if self.kind == 'listen':
                text = self.backend.listen(goal.request.language, goal.request.max_duration,
                                           self.cancel, report)
                if not text.strip():
                    raise ValueError('No speech was recognized')
                result.transcription = text
            else:
                self.backend.talk(goal.request.text, self.cancel, report)
        except Exception as caught:
            error = caught
        try:
            if self.cancel.is_set():
                # cancel_callback runs just before rclpy sets the CANCELING state.
                deadline = monotonic() + 1
                while not goal.is_cancel_requested and not self.stopping and monotonic() < deadline:
                    sleep(0.001)
            if goal.is_cancel_requested:
                result.error_message = 'Canceled'
                if self.kind == 'listen':
                    result.transcription = ''
                goal.canceled()
            elif error is not None or self.stopping:
                result.error_message = str(error or 'Server stopping')
                goal.abort()
                self.get_logger().error(result.error_message)
            else:
                result.success = True
                goal.succeed()
            return result
        finally:
            # The backend has closed its stream before another server can acquire audio.
            with self.guard:
                if self.lease is not None:
                    self.lease.close()
                    self.lease = None

    def stop(self):
        self.stopping = True
        self.cancel.set()
        self.backend.stop()


def run(kind, args=None):
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = None
    executor = MultiThreadedExecutor(num_threads=4)
    try:
        node = SpeechServer(kind)
        executor.add_node(node)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.stop()
        executor.shutdown()
        if node is not None:
            node.server.destroy()
            node.destroy_node()
        rclpy.shutdown()


def listen_main(args=None):
    run('listen', args)


def talk_main(args=None):
    run('talk', args)
