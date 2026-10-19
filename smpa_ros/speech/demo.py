"""A sequential action client: listen, say 'I heard ...', repeat."""
from time import sleep

from action_msgs.msg import GoalStatus
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from smpa_ros_interfaces.action import Listen, Talk


class SpeechActionFailed(RuntimeError):
    pass


class ListenerTalker(Node):
    def __init__(self):
        super().__init__('listener_talker')
        self.declare_parameter('language', 'en')
        self.declare_parameter('max_duration', 10.0)
        self.declare_parameter('action_timeout', 120.0)
        self.declare_parameter('turns', 0)
        self.listener = ActionClient(self, Listen, '/speech/listen')
        self.talker = ActionClient(self, Talk, '/speech/talk')
        self._spin_executor = SingleThreadedExecutor()
        self._spin_executor.add_node(self)
        self.active = None
        self.pending_goal = None

    def wait(self, future, timeout):
        self._spin_executor.spin_until_future_complete(future, timeout_sec=timeout)
        if not future.done():
            raise TimeoutError('Speech action timed out')
        return future.result()

    def cancel_active(self):
        if self.active is None and self.pending_goal is not None:
            # Resolve acceptance before leaving, so an accepted goal is not abandoned.
            handle = self.wait(self.pending_goal, 5)
            if handle.accepted:
                self.active = handle
        if self.active is not None:
            self.wait(self.active.cancel_goal_async(), 5)
            self.wait(self.active.get_result_async(), 5)
            self.active = None

    def perform(self, client, goal):
        timeout = self.get_parameter('action_timeout').value
        if not client.wait_for_server(timeout_sec=timeout):
            raise TimeoutError('Speech action server unavailable')
        self.pending_goal = client.send_goal_async(goal)
        try:
            handle = self.wait(self.pending_goal, timeout)
            self.pending_goal = None
            if not handle.accepted:
                raise SpeechActionFailed('Speech goal rejected: audio may be busy')
            self.active = handle
            response = self.wait(handle.get_result_async(), timeout)
            self.active = None
            if response.status != GoalStatus.STATUS_SUCCEEDED or not response.result.success:
                raise SpeechActionFailed(response.result.error_message or 'Speech action did not succeed')
            return response.result
        except BaseException:
            if self.active is not None or self.pending_goal is not None:
                self.cancel_active()
            raise

    def turn(self):
        result = self.perform(self.listener, Listen.Goal(
            language=self.get_parameter('language').value,
            max_duration=self.get_parameter('max_duration').value,
        ))
        text = f'I heard {result.transcription}'
        # The Listen result is terminal (and capture is closed) before Talk starts.
        self.perform(self.talker, Talk.Goal(text=text))
        return text

    def run(self):
        turns = self.get_parameter('turns').value
        count = 0
        while rclpy.ok() and (turns == 0 or count < turns):
            try:
                self.get_logger().info(self.turn())
                count += 1
            except SpeechActionFailed as error:
                self.get_logger().warning(str(error))
                sleep(0.5)

    def close(self):
        try:
            self.cancel_active()
        finally:
            self.listener.destroy()
            self.talker.destroy()
            self._spin_executor.shutdown()
            self.destroy_node()


def main(args=None):
    # Keep the ROS context alive long enough to cancel on Ctrl+C.
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = ListenerTalker()
    try:
        node.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.close()
        finally:
            rclpy.shutdown()
