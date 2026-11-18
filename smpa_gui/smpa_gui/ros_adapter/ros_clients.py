"""Call ROS 2 services and actions from asynchronous web code, and relay topics, through the gateway's node.

Concepts:
    A programmatic call is a service call or an action goal made by the
    GUI's own backend (the Dialogue coordinator, the SMPA status view), as
    opposed to a call a user confirms in a dialog (``ros_services``,
    ``action_goals``). It is described by an endpoint name, an interface
    type name such as ``smpa_ros_interfaces/action/GenerateText`` and the
    request or goal as plain JSON data. The message classes come from the
    installed interface packages; nothing is redefined here.

    Awaiting: the gateway's node and its single executor thread stay the
    only place where ROS entities are created and where ROS callbacks run.
    Each ROS future is bridged to an asyncio future of the web server's
    event loop, so a coroutine waiting for a long action does not block the
    event loop or any other request.

    Clients are short-lived: a client is created for one call and destroyed
    when the call ends, so an idle GUI holds no clients on the ROS graph
    (the same rule as parameter reading and action goals).

    Outcome: a call either returns the response or result as JSON data, or
    raises ``RosCallFailed`` whose ``state`` says why: ``interface_missing``
    (the type's package is not installed), ``invalid_request`` (the values
    do not fit the type), ``unavailable`` (no server answers the endpoint),
    ``rejected`` (the action server refused the goal), ``timed_out`` (no
    answer in time; the outcome is unknown), ``aborted`` or ``canceled``
    (the action server ended the goal so), or ``failed``. Calls are never
    retried here.

    A topic relay subscribes to a topic for as long as someone listens and
    hands every message, as JSON data, to a callback.
"""
import asyncio
import threading
import time
import uuid

from smpa_gui.ros_adapter import ros_interfaces, ros_services, ros_topics
from smpa_gui.ros_adapter.gateway import GatewayError

ACTION_STATES = {4: 'succeeded', 5: 'canceled', 6: 'aborted'}


class RosCallFailed(GatewayError):
    """A programmatic ROS call did not succeed; ``state`` names the reason and the message explains it."""

    def __init__(self, state, message, **details):
        """Keep the reason, the explanation and any details (such as a partial result) for the caller."""
        super().__init__(message)
        self.state = state
        self.details = details


def bridge(loop, ros_future):
    """Return an asyncio future of ``loop`` that completes when a ROS future completes.

    Must be called on the ROS thread, which is where the ROS future's done
    callback runs. The callback hands the outcome (result or exception) to
    the event loop with ``call_soon_threadsafe``; a waiting coroutine that
    gave up meanwhile is left alone.
    """
    waiting = loop.create_future()

    def settle(done):
        """Copy the ROS future's outcome into the asyncio future, unless it was cancelled."""
        if waiting.done():
            return
        try:
            waiting.set_result(done.result())
        except BaseException as error:  # handed to the waiting coroutine
            waiting.set_exception(error)

    ros_future.add_done_callback(lambda done: loop.call_soon_threadsafe(settle, done))
    return waiting


class RosClients:
    """Make programmatic service calls, action goals and topic relays through one gateway."""

    def __init__(self, gateway):
        """Remember the gateway and start with no live clients and no relays.

        ``live`` holds every client that exists right now, so ``shutdown``
        can destroy them if the GUI stops in the middle of a call.
        """
        self.gateway = gateway
        self.lock = threading.Lock()
        self.live = set()
        self.relays = {}

    async def on_ros(self, function, timeout=5.0):
        """Run a function on the ROS thread without blocking the event loop, and return its result.

        ``Gateway.call`` waits on the calling thread, so it runs in a worker
        thread here. A stopped gateway raises ``GatewayError``.
        """
        return await asyncio.to_thread(self.gateway.call, function, timeout)

    async def create(self, kind, type_class, name):
        """Create a service client (kind "service") or an action client (kind "action") on the ROS thread."""
        def make():
            """Create the client and remember it as live."""
            if kind == 'service':
                client = self.gateway.node.create_client(type_class, name)
            else:
                from rclpy.action import ActionClient
                client = ActionClient(self.gateway.node, type_class, name)
            with self.lock:
                self.live.add(client)
            return client
        return await self.on_ros(make)

    async def destroy(self, client):
        """Destroy one client on the ROS thread; a stopped gateway has already released it."""
        def remove():
            """Forget and destroy the client."""
            with self.lock:
                if client not in self.live:
                    return
                self.live.discard(client)
            client.destroy() if hasattr(client, 'wait_for_server') else self.gateway.node.destroy_client(client)
        try:
            await self.on_ros(remove)
        except GatewayError:
            pass

    async def wait_for_server(self, client, patience):
        """Wait up to ``patience`` seconds for the client to see a server; return whether it did.

        A new client needs a moment to match a server that already exists,
        so readiness is polled in short steps instead of failing at once.
        """
        ready = client.server_is_ready if hasattr(client, 'server_is_ready') else client.service_is_ready
        deadline = time.monotonic() + patience
        while True:
            if await self.on_ros(ready):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)

    async def call_service(self, name, type_name, request=None, timeout=5.0, patience=1.0):
        """Call one service with a request given as JSON data and return the response as JSON data.

        The service class is looked up from the installed interfaces and the
        request is built and checked against it before anything is sent.
        Then a client is created, given up to ``patience`` seconds to find
        the server, and the request is sent once. The answer is awaited for
        at most ``timeout`` seconds; without an answer the call is
        ``timed_out`` (the server may still act on it). The client is
        destroyed in every case.
        """
        try:
            service = ros_services.service_class(type_name)
        except GatewayError as error:
            raise RosCallFailed('interface_missing', str(error)) from error
        try:
            message = ros_interfaces.build(service.Request, request or {})
        except ros_interfaces.InvalidValue as error:
            raise RosCallFailed('invalid_request', f'The request does not fit {type_name}', errors=error.errors) from error
        loop = asyncio.get_running_loop()
        client = await self.create('service', service, name)
        try:
            if not await self.wait_for_server(client, patience):
                raise RosCallFailed('unavailable', f'No server answers the service {name}')
            holder = {}

            def send():
                """Send the request on the ROS thread and bridge its future."""
                holder['future'] = client.call_async(message)
                return bridge(loop, holder['future'])
            waiting = await self.on_ros(send)
            try:
                response = await asyncio.wait_for(waiting, timeout)
            except asyncio.TimeoutError as error:
                try:
                    await self.on_ros(lambda: client.remove_pending_request(holder['future']))
                except GatewayError:
                    pass
                raise RosCallFailed('timed_out', f'The service {name} did not answer within {timeout:g} s; '
                                    'it is unknown whether it acted') from error
            return ros_interfaces.to_json(response)
        finally:
            await self.destroy(client)

    async def run_action(self, name, type_name, goal=None, timeout=60.0, patience=1.0, on_event=None):
        """Send one action goal given as JSON data, wait for its result and return it as JSON data.

        The action class is looked up from the installed interfaces and the
        goal is built and checked against it. A client is created and given
        up to ``patience`` seconds to find the server; then the goal is sent
        once. ``on_event`` (optional) is called on the event loop with
        ``('accepted', {goal_id})`` and ``('feedback', {feedback})`` as they
        happen. The whole goal, acceptance included, may take ``timeout``
        seconds; after that a cancellation is requested and the call is
        ``timed_out``. A refused goal is ``rejected``; a goal the server
        aborted or cancelled raises with that state and the result it gave.
        The client is destroyed in every case. Returns ``{goal_id, state,
        result}`` for a succeeded goal.
        """
        from unique_identifier_msgs.msg import UUID
        try:
            action = ros_interfaces.action_class(type_name)
        except GatewayError as error:
            raise RosCallFailed('interface_missing', str(error)) from error
        try:
            message = ros_interfaces.build(action.Goal, goal or {})
        except ros_interfaces.InvalidValue as error:
            raise RosCallFailed('invalid_request', f'The goal does not fit {type_name}', errors=error.errors) from error
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        identifier = uuid.uuid4()
        notify = on_event or (lambda kind, details: None)

        def feedback(received):
            """Hand one feedback message from the ROS thread to the event loop."""
            loop.call_soon_threadsafe(notify, 'feedback', dict(feedback=ros_topics.plain(received.feedback)))

        client = await self.create('action', action, name)
        handle = None
        try:
            if not await self.wait_for_server(client, patience):
                raise RosCallFailed('unavailable', f'No server answers the action {name}')

            def accepted(done):
                """Announce acceptance from the ROS thread, so it reaches the event loop before any feedback."""
                try:
                    if done.result().accepted:
                        loop.call_soon_threadsafe(notify, 'accepted', dict(goal_id=str(identifier)))
                except Exception:  # noqa: BLE001 - the waiting coroutine reports the failure
                    pass

            def send():
                """Send the goal on the ROS thread and bridge the acceptance future."""
                future = client.send_goal_async(message, feedback_callback=feedback,
                                                goal_uuid=UUID(uuid=list(identifier.bytes)))
                future.add_done_callback(accepted)
                return bridge(loop, future)
            remaining = lambda: max(0.0, timeout - (time.monotonic() - started))
            try:
                handle = await asyncio.wait_for(await self.on_ros(send), remaining())
            except asyncio.TimeoutError as error:
                raise RosCallFailed('timed_out', f'The action {name} did not accept or refuse the goal within '
                                    f'{timeout:g} s') from error
            if not handle.accepted:
                raise RosCallFailed('rejected', f'The action server {name} rejected the goal')
            try:
                response = await asyncio.wait_for(await self.on_ros(lambda: bridge(loop, handle.get_result_async())),
                                                  remaining())
            except asyncio.TimeoutError as error:
                try:
                    await self.on_ros(lambda: handle.cancel_goal_async())
                except GatewayError:
                    pass
                raise RosCallFailed('timed_out', f'The action {name} did not finish within {timeout:g} s; '
                                    'a cancellation was requested and the outcome is unknown',
                                    goal_id=str(identifier)) from error
            state = ACTION_STATES.get(response.status, 'failed')
            result = ros_interfaces.to_json(response.result)
            if state != 'succeeded':
                raise RosCallFailed(state, f'The action server {name} ended the goal as {state}',
                                    goal_id=str(identifier), result=result)
            return dict(goal_id=str(identifier), state=state, result=result)
        finally:
            await self.destroy(client)

    def relay(self, topic, type_name, callback):
        """Subscribe to a topic and hand every message, as JSON data, to ``callback``; return a release function.

        ``callback`` runs on the ROS thread and must only hand the message
        on (for example with ``call_soon_threadsafe``). Several listeners of
        the same topic share one subscription, which is destroyed when the
        last one releases. An unknown type raises ``RosCallFailed`` with the
        state ``interface_missing``; nothing is subscribed then.
        """
        key = (topic, type_name)

        def subscribe():
            """Create or share the subscription on the ROS thread and add the listener."""
            with self.lock:
                entry = self.relays.get(key)
                if entry is None:
                    message_type = ros_topics.message_class(type_name)
                    entry = dict(listeners=[])

                    def deliver(message):
                        """Give one received message to every current listener."""
                        data = ros_topics.plain(message)
                        for listener in list(entry['listeners']):
                            listener(data)
                    entry['subscription'] = self.gateway.node.create_subscription(
                        message_type, topic, deliver, ros_topics.subscription_qos(self.gateway.node, topic))
                    self.relays[key] = entry
                entry['listeners'].append(callback)

        def unsubscribe():
            """Remove the listener and destroy the subscription when nobody listens any more."""
            with self.lock:
                entry = self.relays.get(key)
                if entry is None or callback not in entry['listeners']:
                    return
                entry['listeners'].remove(callback)
                if not entry['listeners']:
                    del self.relays[key]
                    self.gateway.node.destroy_subscription(entry['subscription'])

        try:
            self.gateway.call(subscribe)
        except GatewayError as error:
            raise RosCallFailed('interface_missing' if 'not available' in str(error) else 'failed', str(error)) from error

        def release():
            """End this listener's relay; harmless after the gateway stopped."""
            try:
                self.gateway.call(unsubscribe)
            except GatewayError:
                pass
        return release

    def shutdown(self):
        """Destroy every client and relay subscription that still exists; used when the GUI stops.

        Calls that are still waiting then end with an error. Runs before the
        gateway leaves the ROS graph.
        """
        def clear():
            """Destroy all live clients and relay subscriptions on the ROS thread."""
            with self.lock:
                clients, self.live = list(self.live), set()
                relays, self.relays = list(self.relays.values()), {}
            for client in clients:
                client.destroy() if hasattr(client, 'wait_for_server') else self.gateway.node.destroy_client(client)
            for entry in relays:
                self.gateway.node.destroy_subscription(entry['subscription'])
        try:
            self.gateway.call(clear, timeout=2.0)
        except GatewayError:
            pass

    def counts(self):
        """Tell how many clients and relay subscriptions exist right now (for tests and diagnostics)."""
        with self.lock:
            return dict(clients=len(self.live), relays=len(self.relays))
