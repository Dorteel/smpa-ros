"""Own the single ROS 2 node, its spinning thread, and on-demand subscriptions.

Concepts:
    The ROS thread is the only thread that creates, destroys, or queries ROS
    entities. Web request threads hand it small functions through ``call``
    and wait for the answer. This keeps rclpy usage single-threaded.

    A slot is one live subscription to one (topic, type) pair. Several
    browser views may share a slot; it remembers only the newest message and
    is destroyed when its last viewer releases it. Nothing is subscribed
    while no view is open.
"""
import os
import threading
import time

from backend.ros_names import full_node_name


class GatewayError(RuntimeError):
    """A ROS request could not be completed; the message is safe to show."""


class Slot:
    """Hold the newest message of one shared subscription."""

    def __init__(self, topic, type_name):
        """Remember what is subscribed and start with no message and no viewers.

        The sequence number counts received messages, so a stream can tell a
        new message from one it already sent. The receive times of the last
        few messages allow an approximate rate to be shown.
        """
        self.topic = topic
        self.type_name = type_name
        self.subscription = None
        self.viewers = 0
        self.message = None
        self.sequence = 0
        self.received_at = None
        self.recent = []

    def store(self, message):
        """Keep only the newest message and note when it arrived.

        This runs on the ROS thread for every received message. Older
        messages are dropped on purpose: views show the present, not a queue.
        """
        now = time.monotonic()
        self.message = message
        self.sequence += 1
        self.received_at = now
        self.recent = [stamp for stamp in self.recent if now - stamp < 2.0] + [now]

    def rate(self):
        """Estimate how many messages per second arrived during the last two seconds.

        Return zero when fewer than two recent messages exist or when the
        newest message is older than two seconds.
        """
        now = time.monotonic()
        recent = [stamp for stamp in self.recent if now - stamp < 2.0]
        if len(recent) < 2:
            return 0.0
        return round((len(recent) - 1) / (recent[-1] - recent[0]), 2)


class Gateway:
    """Run one observational ROS 2 node beside the web server."""

    def __init__(self, node_name='smpa_gui'):
        """Prepare an unstarted gateway with the given ROS node name.

        No ROS context exists until ``start`` is called, so constructing a
        gateway has no effect on the ROS graph.
        """
        self.node_name = node_name
        self.context = None
        self.node = None
        self.executor = None
        self.thread = None
        self.slots = {}
        self.slot_lock = threading.Lock()
        self.cache = {}
        self.cache_lock = threading.Lock()

    def start(self):
        """Join the ROS graph as one node and begin spinning in a background thread.

        A private rclpy context is used so that the web server keeps control
        of process signals. The node starts without parameter services,
        without the type-description service and without a log publisher,
        because the gateway only observes. Calling
        ``start`` twice is an error rather than a second node.
        """
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.parameter import Parameter
        from rclpy.signals import SignalHandlerOptions

        if self.node is not None:
            raise GatewayError('The gateway is already running')
        self.context = rclpy.Context()
        rclpy.init(context=self.context, signal_handler_options=SignalHandlerOptions.NO)
        quiet = [Parameter('start_type_description_service', Parameter.Type.BOOL, False)]
        self.node = rclpy.create_node(self.node_name, context=self.context, parameter_overrides=quiet,
                                      start_parameter_services=False, enable_rosout=False)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self._spin, name='ros-gateway', daemon=True)
        self.thread.start()

    def _spin(self):
        """Process ROS callbacks until the context is shut down.

        Shutdown from another thread makes ``spin`` raise or return; both are
        normal endings here, so the thread simply finishes.
        """
        from rclpy.executors import ExternalShutdownException
        try:
            self.executor.spin()
        except (ExternalShutdownException, KeyboardInterrupt):
            pass
        except Exception:
            # A failure after shutdown began is expected; otherwise re-raise.
            if self.context is not None and self.context.ok():
                raise

    def stop(self):
        """Leave the ROS graph and release every ROS resource the gateway owns.

        Destroy remaining subscriptions on the ROS thread first, then shut
        the executor and context down and wait briefly for the thread. Other
        ROS processes are unaffected. Stopping an unstarted gateway does nothing.
        """
        if self.node is None:
            return
        try:
            self.call(self._destroy_all_slots, timeout=2.0)
        except GatewayError:
            pass
        self.executor.shutdown(timeout_sec=2.0)
        self.node.destroy_node()
        if self.context.ok():
            self.context.shutdown()
        self.thread.join(timeout=2.0)
        self.node = self.executor = self.thread = self.context = None

    def _destroy_all_slots(self):
        """Destroy every live subscription; used only while stopping."""
        with self.slot_lock:
            for slot in self.slots.values():
                if slot.subscription is not None:
                    self.node.destroy_subscription(slot.subscription)
            self.slots.clear()

    def running(self):
        """Report whether the node exists and its ROS context is still valid."""
        return self.node is not None and self.context is not None and self.context.ok()

    def full_name(self):
        """Return the gateway node's fully qualified ROS name."""
        return full_node_name(self.node.get_namespace(), self.node.get_name())

    def describe(self):
        """Summarise the ROS environment this process joined, for the health check.

        The domain and middleware decide which other processes are visible,
        so they are reported exactly as this process sees them.
        """
        from rclpy.utilities import get_rmw_implementation_identifier
        return dict(ok=self.running(), node=self.full_name() if self.node else None,
                    domain_id=int(os.environ.get('ROS_DOMAIN_ID', '0') or 0),
                    rmw=get_rmw_implementation_identifier(),
                    distro=os.environ.get('ROS_DISTRO'))

    def call(self, function, timeout=5.0):
        """Run a function on the ROS thread and return its result to the caller.

        The function is queued as an executor task. The calling thread waits
        until it finishes or the timeout passes. An exception raised by the
        function is raised again here. A stopped gateway or a timeout raises
        ``GatewayError`` instead of blocking a web request forever.
        """
        if not self.running():
            raise GatewayError('The ROS gateway is not running')
        finished = threading.Event()
        outcome = {}

        def run():
            """Execute the queued function and record its result or error."""
            try:
                outcome['value'] = function()
            except BaseException as error:  # reported to the waiting caller
                outcome['error'] = error
            finally:
                finished.set()

        self.executor.create_task(run)
        self.executor.wake()
        if not finished.wait(timeout):
            raise GatewayError('The ROS gateway did not answer in time')
        if 'error' in outcome:
            raise outcome['error']
        return outcome.get('value')

    def cached(self, key, produce, lifetime=1.0):
        """Return a recent result for ``key`` or produce and remember a new one.

        Graph discovery is shared by several views that poll at once. Reusing
        a result for about a second bounds the discovery work regardless of
        how many browser tabs are open.
        """
        now = time.monotonic()
        with self.cache_lock:
            entry = self.cache.get(key)
            if entry is not None and now - entry[0] < lifetime:
                return entry[1]
        value = produce()
        with self.cache_lock:
            self.cache[key] = (time.monotonic(), value)
        return value

    def acquire(self, topic, type_name):
        """Give a viewer access to the newest messages of a topic.

        If another viewer already watches the same topic and type, share its
        slot. Otherwise resolve the message class and create a subscription
        whose quality of service is compatible with the current publishers.
        An unknown message type raises ``GatewayError`` and subscribes to
        nothing. Every successful acquire must be paired with one release.
        """
        key = (topic, type_name)

        def create():
            """Create or share the slot on the ROS thread."""
            with self.slot_lock:
                slot = self.slots.get(key)
                if slot is None:
                    from backend.ros_topics import message_class, subscription_qos
                    cls = message_class(type_name)
                    slot = Slot(topic, type_name)
                    slot.subscription = self.node.create_subscription(
                        cls, topic, slot.store, subscription_qos(self.node, topic))
                    self.slots[key] = slot
                slot.viewers += 1
                return slot

        return self.call(create)

    def release(self, slot):
        """End one viewer's use of a slot and unsubscribe when nobody is left.

        The subscription is destroyed on the ROS thread as soon as the last
        viewer releases it. Releasing after the gateway stopped is harmless.
        """
        def drop():
            """Decrease the viewer count and destroy an unused subscription."""
            with self.slot_lock:
                slot.viewers -= 1
                if slot.viewers <= 0:
                    self.slots.pop((slot.topic, slot.type_name), None)
                    if slot.subscription is not None:
                        self.node.destroy_subscription(slot.subscription)
                        slot.subscription = None

        try:
            self.call(drop)
        except GatewayError:
            pass

    def active_subscriptions(self):
        """List what the gateway is subscribed to right now, with viewer counts."""
        with self.slot_lock:
            return [dict(topic=slot.topic, type=slot.type_name, viewers=slot.viewers)
                    for slot in self.slots.values()]
