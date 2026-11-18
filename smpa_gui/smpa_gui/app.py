"""Serve the documented HTTP and WebSocket contract and the static frontend.

Concepts:
    A snapshot endpoint answers one question about the ROS graph as JSON.
    A stream is a WebSocket on which the gateway sends the newest message of
    one topic at a bounded rate, together with status messages that explain
    silence (no publishers, waiting, unsupported data). Opening a stream
    subscribes; closing it unsubscribes.

    Everything is read-only except four groups of change routes: sending and
    cancelling action goals (``/api/v1/actions/goals``), setting parameters
    (``/api/v1/ros/parameters/set``), calling services
    (``/api/v1/ros/services/call``) and the SMPA conversation routes
    (``/api/v1/smpa/conversation/...``, which relay to SMPA's own
    conversation server). Each is made only after an explicit
    confirmation in the page, accepted only from this computer, only with the
    session token of this gateway process, never repeated for the same
    request id, and all of them can be switched off together (read-only
    mode). There is no route for publishing on topics.
"""
import asyncio
import ipaddress
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from typing import Any

from pydantic import BaseModel, Field

from smpa_gui import CONTRACT_VERSION
from smpa_gui.ros_adapter import ros_graph, ros_interfaces, ros_parameters, ros_services, ros_topics, sensors
from smpa_gui.smpa_adapter import conversation_transcript, rdf_graphs, smpa_layout, smpa_models, smpa_runs
from smpa_gui.smpa_adapter.conversation_service import ConversationError, ConversationService
from smpa_gui.ros_adapter.action_goals import GoalManager, GoalRefused
from smpa_gui.ros_adapter.gateway import Gateway, GatewayError

FRONTEND = Path(__file__).resolve().parent / 'frontend'
LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')
SILENCE_SECONDS = 2.0


def envelope(**fields):
    """Add the contract version to a response body."""
    return dict(contract_version=CONTRACT_VERSION, **fields)


def bounded_rate(requested, default, highest):
    """Choose a stream rate in messages per second within safe limits.

    Use the default when the client asked for nothing, and never go below
    0.2 or above the highest rate allowed for that kind of stream.
    """
    return max(0.2, min(float(requested if requested else default), float(highest)))


def local_origin(origin):
    """Decide whether a browser page from this origin may open a stream.

    Non-browser clients send no origin and are accepted. A browser page is
    accepted only when it was itself loaded from this computer, so a web page
    from elsewhere cannot read local ROS data through the user's browser.
    """
    return origin is None or urlsplit(origin).hostname in LOCAL_HOSTS


def is_loopback(host):
    """Tell whether a network address belongs to this computer itself."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ParameterChange(BaseModel):
    """One proposed parameter value: the parameter's name, the ROS parameter type and the new value."""

    name: str = Field(min_length=1, max_length=256)
    type: str = Field(pattern=r'^(bool|integer|double|string|byte_array|bool_array|integer_array|double_array|string_array)$')
    value: Any


class ParameterChangeRequest(BaseModel):
    """A confirmed set of parameter changes for one node, applied atomically or one by one."""

    node: str = Field(min_length=2, max_length=512, pattern=r'^/')
    changes: list[ParameterChange] = Field(min_length=1, max_length=50)
    atomic: bool = True
    request_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


class ServiceCallRequest(BaseModel):
    """A confirmed service call: service name, type, request values, request id and a bounded timeout."""

    service: str = Field(min_length=2, max_length=512, pattern=r'^/')
    type: str = Field(min_length=3, max_length=256)
    request: dict
    request_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    timeout: float = Field(default=10.0, ge=0.5, le=60.0)


class ConversationStart(BaseModel):
    """Start a new conversation with a model source such as ``local:qwen3-4b`` or ``api:openai``."""

    source: str | None = Field(default=None, max_length=200)
    request_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


class ConversationResume(BaseModel):
    """Reopen an existing conversation session, optionally with another model."""

    session_id: str = Field(min_length=1, max_length=200)
    source: str | None = Field(default=None, max_length=200)
    request_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


class ConversationModel(BaseModel):
    """Switch the active conversation to another model source."""

    source: str = Field(min_length=3, max_length=200)


class ConversationMessage(BaseModel):
    """One user message for the active conversation."""

    text: str = Field(min_length=1, max_length=32000)
    request_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


class GoalSubmission(BaseModel):
    """The body of a goal submission: which action, its type, the goal values and a request identifier."""

    action: str = Field(min_length=2, max_length=512, pattern=r'^/')
    type: str = Field(min_length=3, max_length=256)
    goal: dict
    request_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


def create_app(gateway=None, *, allow_changes=True, cancel_on_exit=True, memory_root=None, smpa_enabled=True,
               layout=None, conversation=None):
    """Build the web application around one gateway.

    The gateway joins ROS when the application starts and leaves when it
    stops, so starting and stopping the GUI never depends on other ROS
    processes. A gateway may be supplied by tests; otherwise one is created.
    ``allow_changes`` false makes the whole application read-only: no action
    goals, no parameter changes, no service calls.
    ``cancel_on_exit`` decides whether goals that are still active when the
    application stops are asked to cancel. ``memory_root`` is SMPA's memory
    directory, read (never written) by the SMPA tab; None leaves it unconfigured.
    ``smpa_enabled`` false disables the optional SMPA integration altogether:
    the SMPA routes then say that it is disabled, read nothing and start
    nothing. ``layout`` is the GUI's SMPA path configuration
    (``smpa_layout.load()`` when None); its memory directory is used when
    ``memory_root`` is not given. ``conversation`` is the adapter to SMPA's
    conversation server (created from the layout when None); a server it
    started is stopped with the application.
    """
    layout = dict(layout or smpa_layout.load())
    if memory_root:
        layout['memory_root'] = Path(memory_root).absolute()
    elif smpa_enabled and layout['smpa_root'] is not None:
        memory_root = layout['memory_root']
    conversation = conversation or ConversationService(layout)
    gateway = gateway or Gateway()
    session_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        """Start the ROS gateway before serving; on shutdown settle active goals, then stop it."""
        gateway.start()
        app.state.goals = GoalManager(gateway)
        app.state.parameter_writer = ros_parameters.ParameterWriter(gateway)
        app.state.service_caller = ros_services.ServiceCaller(gateway)
        try:
            yield
        finally:
            await asyncio.to_thread(app.state.goals.shutdown, cancel_on_exit)
            await asyncio.to_thread(conversation.shutdown)
            gateway.stop()
            print('SMPA-ROS GUI stopped: goals settled, node left the ROS graph.', flush=True)
            # A second Ctrl+C (a terminal and ros2 launch both send one) makes uvicorn
            # abandon its already finished lifespan task and log that as an error.
            # Everything is settled at this point, so that report is silenced.
            logging.getLogger('uvicorn.error').setLevel(logging.CRITICAL)

    app = FastAPI(title='SMPA-ROS GUI gateway', version=CONTRACT_VERSION, lifespan=lifespan,
                  docs_url='/api/docs', redoc_url=None, openapi_url='/api/openapi.json')
    app.state.gateway = gateway

    def graph():
        """Return the current graph description or a 503 when ROS is unavailable."""
        try:
            return ros_graph.snapshot(gateway)
        except GatewayError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    def resolve_type(topic, type_name):
        """Return the message type to use for a topic.

        An explicitly requested type wins. Otherwise use the topic's first
        discovered type. Return ``None`` when the topic is unknown and no
        type was given; the caller then reports that nothing can be shown yet.
        """
        if type_name:
            return type_name
        info = ros_topics.topic_info(graph(), topic)
        return info['types'][0] if info and info['types'] else None

    def release_later(slot):
        """Release a slot on a worker thread without waiting for it.

        Streams end in many ways, including cancellation, and a cancelled
        task cannot wait for anything. Handing the release to a thread makes
        sure the subscription is dropped however the stream ended.
        """
        asyncio.get_running_loop().run_in_executor(None, gateway.release, slot)

    def publisher_count(topic):
        """Count the topic's current publishers; zero when it is unknown."""
        info = ros_topics.topic_info(graph(), topic)
        return info['publisher_count'] if info else 0

    @app.get('/api/v1/health')
    def health():
        """Report that the server runs and which ROS environment it joined."""
        return envelope(status='ok', ros=gateway.describe(),
                        active_subscriptions=gateway.active_subscriptions())

    @app.get('/api/v1/ros/graph')
    def get_graph():
        """Return nodes, topics, connections, actions and services."""
        return graph()

    @app.get('/api/v1/ros/topics')
    def get_topics():
        """List topics with their types and publisher and subscriber counts."""
        return envelope(topics=graph()['topics'])

    @app.get('/api/v1/ros/topics/info')
    def get_topic_info(name: str = Query(...)):
        """Describe one topic's publishers and subscribers with their QoS."""
        info = ros_topics.topic_info(graph(), name)
        if info is None:
            raise HTTPException(status_code=404, detail=f'Topic {name} is not currently known')
        return envelope(topic=info)

    @app.get('/api/v1/ros/nodes')
    def get_nodes():
        """List the nodes currently discovered."""
        return envelope(nodes=graph()['nodes'])

    @app.get('/api/v1/ros/nodes/parameters')
    def get_parameters(node: str = Query(...), timeout: float = Query(2.0, ge=0.2, le=10.0)):
        """Read one node's parameters; an unresponsive node is a state, not an error."""
        try:
            return envelope(**ros_parameters.read(gateway, node, timeout))
        except GatewayError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get('/api/v1/ros/parameters/changes')
    def get_parameter_changes():
        """List the parameter changes made through this GUI process, newest first."""
        return envelope(changes=app.state.parameter_writer.list())

    @app.post('/api/v1/ros/parameters/set')
    def post_parameter_change(body: ParameterChangeRequest, request: Request):
        """Apply one confirmed set of parameter changes and return the node's answers and the values read back.

        Problems with the proposed values give 422 with one entry per
        problem and change nothing; a node that does not answer gives 409.
        A repeated request id returns the first record and changes nothing.
        """
        authorised(request)
        names = [change.name for change in body.changes]
        if len(set(names)) != len(names):
            raise HTTPException(status_code=422, detail='Each parameter may appear only once in a request')
        try:
            record = app.state.parameter_writer.apply(body.node, [change.model_dump() for change in body.changes],
                                                      body.atomic, body.request_id)
        except ros_parameters.ParameterChangeRefused as error:
            return JSONResponse(status_code=error.status, content=envelope(detail=str(error), errors=error.errors))
        except GatewayError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return envelope(change=record)

    @app.get('/api/v1/ros/services')
    def get_services():
        """List services with their types, kind, provider nodes and client nodes."""
        try:
            return envelope(services=ros_services.discover(gateway))
        except GatewayError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get('/api/v1/ros/services/interface')
    def get_service_interface(type: str = Query(...)):
        """Describe a service type's Request and Response fields from its interface definition."""
        try:
            return envelope(**ros_services.interface(type))
        except GatewayError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get('/api/v1/ros/services/calls')
    def get_service_calls():
        """List the service calls made through this GUI process, newest first."""
        return envelope(calls=app.state.service_caller.list())

    @app.get('/api/v1/ros/services/calls/{call_id}')
    def get_service_call(call_id: str):
        """Return one service call record."""
        record = app.state.service_caller.get(call_id)
        if record is None:
            raise HTTPException(status_code=404, detail='Unknown service call identifier')
        return envelope(call=record)

    @app.post('/api/v1/ros/services/call')
    def post_service_call(body: ServiceCallRequest, request: Request):
        """Call one service once and answer when it responded, timed out, or its provider vanished.

        Invalid request values give 422 with one entry per wrong field; no
        provider gives 409; an unknown type gives 404. A repeated request id
        returns the first record and calls nothing. A timed-out call answers
        504 with its record: the outcome is unknown and nothing is retried.
        """
        authorised(request)
        try:
            record = app.state.service_caller.call(body.service, body.type, body.request, body.request_id, body.timeout)
        except ros_interfaces.InvalidValue as error:
            return JSONResponse(status_code=422, content=envelope(detail='The request does not fit the service type',
                                                                  errors=error.errors))
        except ros_services.ServiceCallRefused as error:
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        except GatewayError as error:
            raise HTTPException(status_code=404 if 'not available' in str(error) else 503, detail=str(error)) from error
        status = 504 if record['state'] in ('timed_out', 'server_lost') else 200
        return JSONResponse(status_code=status, content=envelope(call=record))

    @app.get('/api/v1/ros/actions')
    def get_actions():
        """List actions with their types, server nodes and client nodes."""
        return envelope(actions=graph()['actions'])

    @app.get('/api/v1/sensors/registry')
    def get_sensor_registry():
        """List the sensor message types the gateway can visualise."""
        return envelope(sensors=sensors.registry_description())

    @app.get('/api/v1/sensors/topics')
    def get_sensor_topics():
        """List discovered topics that a sensor viewer can display."""
        return envelope(topics=sensors.sensor_topics(graph()))

    async def stream(websocket, topic, type_name, encode, rate):
        """Send the newest messages of one topic until the client disconnects.

        Refuse pages from other origins. Subscribe through a shared slot and
        always release it at the end, whatever ends the stream. In a loop at
        the bounded rate: when a new message arrived, encode it off the event
        loop and send a JSON header followed by an optional binary block.
        When data cannot be drawn, say so once as an ``unsupported`` status.
        When nothing arrived for a while, send a status saying whether the
        topic has publishers. The client never needs to send anything; its
        disconnect is noticed through the pending receive.
        """
        if not local_origin(websocket.headers.get('origin')):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        slot = None
        receiver = None
        try:
            type_name = await asyncio.to_thread(resolve_type, topic, type_name)
            if type_name is None:
                await websocket.send_json(dict(kind='status', state='error', topic=topic,
                                               detail='Topic is unknown and no message type was given'))
                return
            slot = await asyncio.to_thread(gateway.acquire, topic, type_name)
            receiver = asyncio.ensure_future(websocket.receive())
            sent_sequence, state, checked = 0, None, time.monotonic()
            while True:
                if receiver.done():
                    if receiver.result().get('type') == 'websocket.disconnect':
                        break
                    receiver = asyncio.ensure_future(websocket.receive())
                now = time.monotonic()
                if slot.sequence != sent_sequence:
                    sent_sequence, message = slot.sequence, slot.message
                    try:
                        header, binary = await asyncio.to_thread(encode, message)
                    except sensors.UnsupportedData as error:
                        if state != 'unsupported':
                            state = 'unsupported'
                            await websocket.send_json(dict(kind='status', state=state, topic=topic,
                                                           detail=str(error)))
                    else:
                        state = 'streaming'
                        await websocket.send_json(dict(header, kind='data', topic=topic, type=type_name,
                                                       rate_hz=slot.rate()))
                        if binary is not None:
                            await websocket.send_bytes(binary)
                elif ((slot.received_at is None or now - slot.received_at > SILENCE_SECONDS)
                      and now - checked > SILENCE_SECONDS):
                    checked = now
                    publishers = await asyncio.to_thread(publisher_count, topic)
                    silent = 'waiting' if publishers else 'no_publishers'
                    if silent != state:
                        state = silent
                        await websocket.send_json(dict(kind='status', state=state, topic=topic,
                                                       detail='Waiting for the next message' if publishers
                                                       else 'No publisher on this topic'))
                await asyncio.sleep(1.0 / rate)
        except GatewayError as error:
            try:
                await websocket.send_json(dict(kind='status', state='error', topic=topic, detail=str(error)))
            except Exception:
                pass
        except (WebSocketDisconnect, ConnectionError):
            pass  # the client went away while we were sending
        except Exception:
            logging.getLogger(__name__).exception('Stream for %s failed', topic)
        finally:
            if receiver is not None:
                receiver.cancel()
            if slot is not None:
                release_later(slot)
            try:
                await websocket.close()
            except Exception:
                pass

    @app.websocket('/api/v1/ros/topics/echo')
    async def echo(websocket: WebSocket, name: str, type: str = '', rate: float = 0):
        """Stream any topic's messages as bounded JSON for generic inspection."""
        def encode(message):
            """Convert one message to plain data and note whether it was shortened."""
            state = {}
            return dict(message=ros_topics.plain(message, state),
                        truncated=bool(state.get('truncated'))), None

        await stream(websocket, name, type or None, encode, bounded_rate(rate, 5, 20))

    @app.websocket('/api/v1/sensors/stream')
    async def sensor_stream(websocket: WebSocket, topic: str, type: str = '', rate: float = 0,
                            max_width: int = 960, max_points: int = sensors.MAX_POINTS):
        """Stream one sensor topic in the compact form its viewer draws.

        The message type must be in the sensor registry; other types are
        refused with an explanation and remain available through the generic
        echo stream. Image width and point count limits bound the bandwidth.
        """
        type_name = type or await asyncio.to_thread(resolve_type, topic, None)
        entry = sensors.REGISTRY.get(type_name)
        if type_name is not None and entry is None:
            if not local_origin(websocket.headers.get('origin')):
                await websocket.close(code=1008)
                return
            await websocket.accept()
            await websocket.send_json(dict(kind='status', state='error', topic=topic,
                                           detail=f'{type_name} has no sensor viewer; use the Topics view'))
            await websocket.close()
            return
        entry = entry or {}
        width = max(64, min(max_width, 1920))
        points = max(100, min(max_points, 100000))

        def encode(message):
            """Apply the registry encoder with this stream's size limits."""
            if entry['viewer'] == 'image':
                return entry['encode'](message, max_width=width)
            if entry['viewer'] == 'pointcloud':
                return entry['encode'](message, max_points=points)
            return entry['encode'](message)

        await stream(websocket, topic, type_name, encode, bounded_rate(rate, entry.get('rate', 5), 30))

    def image_entry(topic, type_name):
        """Resolve an image topic's type and encoder or raise a clear HTTP error."""
        type_name = resolve_type(topic, type_name)
        entry = sensors.REGISTRY.get(type_name) if type_name else None
        if entry is None or entry['viewer'] != 'image':
            raise HTTPException(status_code=404, detail=f'{topic} is not a known image topic')
        return type_name, entry

    @app.get('/api/v1/sensors/image/frame.jpg')
    def image_frame(topic: str = Query(...), type: str = '', timeout: float = Query(3.0, ge=0.1, le=10.0),
                    max_width: int = Query(960, ge=64, le=1920)):
        """Return one current frame of an image topic as a JPEG file.

        Subscribe, wait up to the timeout for a frame, and unsubscribe again.
        No frame in time gives 504; an encoding that cannot be drawn gives 415.
        """
        type_name, entry = image_entry(topic, type or None)
        try:
            slot = gateway.acquire(topic, type_name)
        except GatewayError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        try:
            deadline = time.monotonic() + timeout
            while slot.message is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if slot.message is None:
                raise HTTPException(status_code=504, detail=f'No image received on {topic}')
            try:
                _, data = entry['encode'](slot.message, max_width=max_width)
            except sensors.UnsupportedData as error:
                raise HTTPException(status_code=415, detail=str(error)) from error
            return Response(content=data, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})
        finally:
            gateway.release(slot)

    @app.get('/api/v1/sensors/image/stream.mjpg')
    async def image_stream(topic: str = Query(...), type: str = '', rate: float = 0,
                           max_width: int = Query(960, ge=64, le=1920)):
        """Serve an image topic as a motion-JPEG stream for non-browser clients.

        Each new frame is one part of a multipart response. The subscription
        lasts exactly as long as the HTTP connection: when the client leaves,
        the web server cancels the generator and its cleanup unsubscribes.
        Frames that cannot be encoded are skipped.
        """
        type_name, entry = await asyncio.to_thread(image_entry, topic, type or None)
        interval = 1.0 / bounded_rate(rate, entry['rate'], 30)

        async def frames():
            """Yield JPEG parts until the client disconnects, then unsubscribe."""
            slot = await asyncio.to_thread(gateway.acquire, topic, type_name)
            sent = 0
            try:
                while True:
                    if slot.sequence != sent:
                        sent = slot.sequence
                        try:
                            _, data = await asyncio.to_thread(entry['encode'], slot.message, max_width)
                        except sensors.UnsupportedData:
                            data = None
                        if data:
                            yield (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                                   + str(len(data)).encode() + b'\r\n\r\n' + data + b'\r\n')
                    await asyncio.sleep(interval)
            finally:
                release_later(slot)

        return StreamingResponse(frames(), media_type='multipart/x-mixed-replace; boundary=frame',
                                 headers={'Cache-Control': 'no-store'})

    def local_request(request):
        """Require that a request comes from a program or page on this computer.

        The connection must originate from a loopback address, even if the
        server was bound to another interface. The Host header must name
        this computer, which defeats DNS rebinding. A browser page must have
        been loaded from this computer. Anything else is refused with 403.
        """
        host = (request.headers.get('host') or '').rsplit(':', 1)[0].strip('[]')
        if (request.client is None or not is_loopback(request.client.host) or host not in LOCAL_HOSTS
                or not local_origin(request.headers.get('origin'))):
            raise HTTPException(status_code=403, detail='Changes (goals, parameters, service calls) are only accepted from this computer')

    def authorised(request):
        """Require everything a change needs: a goal, a cancellation, a parameter change or a service call.

        Changes must be enabled (not read-only), the request must be local,
        its body must be declared as JSON, and it must carry this gateway
        process's session token. The token is handed only to local clients
        and changes whenever the gateway restarts, so a page from another
        site, or a page left over from an earlier gateway, cannot change
        anything.
        """
        if not allow_changes:
            raise HTTPException(status_code=403, detail='Changes are disabled: the GUI runs in read-only mode')
        local_request(request)
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            raise HTTPException(status_code=415, detail='Send the request body as application/json')
        if not secrets.compare_digest(request.headers.get('x-smpa-gui-token', ''), session_token):
            raise HTTPException(status_code=403, detail='Missing or outdated session token; reload the page')

    @app.get('/api/v1/session')
    @app.get('/api/v1/actions/session')
    def get_session(request: Request):
        """Tell a local client whether changes are enabled and give it the session token.

        The same token authorises goals, parameter changes and service calls.
        ``/actions/session`` is the original name and stays for compatibility.
        """
        local_request(request)
        return envelope(enabled=allow_changes, token=session_token if allow_changes else None,
                        cancel_on_exit=cancel_on_exit)

    @app.get('/api/v1/actions/servers')
    def get_action_servers():
        """List discovered actions and whether a server is currently available for each."""
        return envelope(actions=[dict(action, available=bool(action['servers'])) for action in graph()['actions']])

    @app.get('/api/v1/actions/interface')
    def get_action_interface(type: str = Query(...)):
        """Describe an action type's Goal, Feedback and Result fields from its interface definition."""
        try:
            action_type = ros_interfaces.action_class(type)
        except GatewayError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return envelope(type=type, goal=ros_interfaces.describe(action_type.Goal),
                        goal_default=ros_interfaces.defaults(action_type.Goal),
                        feedback=ros_interfaces.describe(action_type.Feedback),
                        result=ros_interfaces.describe(action_type.Result))

    @app.get('/api/v1/actions/goals')
    def get_goals():
        """List the goals this gateway process has submitted, newest first."""
        return envelope(goals=app.state.goals.list())

    @app.get('/api/v1/actions/goals/{goal_id}')
    def get_goal(goal_id: str):
        """Return one submitted goal with its state, feedback and result."""
        record = app.state.goals.get(goal_id)
        if record is None:
            raise HTTPException(status_code=404, detail='Unknown goal identifier')
        return envelope(goal=record)

    @app.post('/api/v1/actions/goals', status_code=201)
    def post_goal(submission: GoalSubmission, request: Request):
        """Send one action goal. Repeating a request identifier returns the first goal and sends nothing.

        Invalid goal values give 422 with one entry per wrong field; a
        missing action server gives 409; an unavailable action type gives 404.
        """
        authorised(request)
        try:
            record = app.state.goals.submit(submission.action, submission.type, submission.goal,
                                            submission.request_id)
        except ros_interfaces.InvalidValue as error:
            return JSONResponse(status_code=422, content=dict(detail='The goal does not fit the action type',
                                                              errors=error.errors))
        except GoalRefused as error:
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        except GatewayError as error:
            raise HTTPException(status_code=404 if 'not available' in str(error) else 503, detail=str(error)) from error
        return JSONResponse(status_code=200 if record.get('duplicate') else 201, content=envelope(goal=record))

    @app.post('/api/v1/actions/goals/{goal_id}/cancel')
    def post_cancel(goal_id: str, request: Request):
        """Ask the action server to cancel one active goal."""
        authorised(request)
        try:
            return envelope(goal=app.state.goals.cancel(goal_id))
        except GoalRefused as error:
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        except GatewayError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    root = Path(memory_root).resolve() if memory_root else None
    app.state.conversation, app.state.layout = conversation, layout

    def smpa_root():
        """Return the configured memory root or answer 404 explaining why there is none.

        With SMPA integration disabled the answer says so; otherwise it
        explains how to configure the memory directory.
        """
        if not smpa_enabled:
            raise HTTPException(status_code=404, detail='SMPA integration is disabled in this GUI (started with --no-smpa)')
        if root is None or not (root / 'runs').is_dir():
            raise HTTPException(status_code=404, detail='No SMPA memory directory is configured '
                                '(start the GUI with --memory-root or SMPA_GUI_MEMORY_ROOT)')
        return root

    def ros_names():
        """Return discovered ROS names for liveness checks, or None when ROS is unavailable."""
        try:
            return smpa_runs.graph_names(ros_graph.snapshot(gateway))
        except GatewayError:
            return None

    @app.get('/api/v1/smpa/status')
    def get_smpa_status():
        """Say whether SMPA integration is enabled, whether its memory directory is configured, and whether it holds demonstration data."""
        configured = smpa_enabled and root is not None and (root / 'runs').is_dir()
        return envelope(enabled=smpa_enabled, configured=configured, memory_root=str(root) if root and smpa_enabled else None,
                        demonstration=bool(configured and (root / smpa_runs.DEMO_MARKER).is_file()))

    @app.get('/api/v1/smpa/sessions')
    def get_smpa_sessions():
        """List SMPA sessions newest first with lifecycle, modalities and activity."""
        directory = smpa_root()
        return envelope(memory_root=str(directory), demonstration=(directory / smpa_runs.DEMO_MARKER).is_file(),
                        sessions=smpa_runs.list_sessions(directory, ros_names()))

    @app.get('/api/v1/smpa/sessions/{session_id}')
    def get_smpa_session(session_id: str):
        """Describe one SMPA session."""
        found = [item for item in smpa_runs.list_sessions(smpa_root(), ros_names()) if item['session_id'] == session_id]
        if not found:
            raise HTTPException(status_code=404, detail='Unknown SMPA session')
        return envelope(session=found[0])

    @app.get('/api/v1/smpa/sessions/{session_id}/observations')
    def get_smpa_observations(session_id: str, modality: str = Query('', pattern='^(|vision|hear|read|sense)$')):
        """Return the recorded observations of a session in chronological order."""
        result = smpa_runs.session_observations(smpa_root(), session_id, modality or None)
        if result is None:
            raise HTTPException(status_code=404, detail='Unknown SMPA session')
        return envelope(**result)

    @app.get('/api/v1/smpa/sessions/{session_id}/files')
    def get_smpa_file(session_id: str, path: str = Query(..., max_length=1024)):
        """Serve one recorded file (image, audio, text) of a session, read-only."""
        found = smpa_runs.recorded_file(smpa_root(), session_id, path)
        if found is None:
            raise HTTPException(status_code=404, detail='No such recorded file in this session')
        return FileResponse(found[0], media_type=found[1], headers={'Cache-Control': 'no-store'})

    def smpa_available():
        """Answer 404 when the optional SMPA integration is disabled, so nothing of SMPA is read or started."""
        if not smpa_enabled:
            raise HTTPException(status_code=404, detail='SMPA integration is disabled in this GUI (started with --no-smpa)')

    def conversation_reply(work):
        """Run a conversation operation and answer with the conversation status, or SMPA's explained error.

        A failed reply still carries the conversation state when SMPA
        returned one (for example: the message was saved but no answer was
        generated), so the page can show what was recorded.
        """
        try:
            work()
        except ConversationError as error:
            return JSONResponse(status_code=error.status, content=envelope(
                detail=str(error), conversation=conversation_status(refresh=False)))
        return envelope(conversation=conversation_status(refresh=False))

    def session_directory(session_id):
        """Return a session's run directory or answer 404."""
        directory = smpa_runs.find_session(smpa_root(), session_id) if session_id != smpa_runs.LEGACY_ID else None
        if directory is None:
            raise HTTPException(status_code=404, detail='Unknown SMPA session')
        return directory

    def conversation_status(refresh=True):
        """Describe the conversation server, the active run's session and its messages."""
        status = conversation.status(refresh=refresh)
        status['messages'] = (conversation.snapshot or {}).get('messages', []) if status['run_id'] else []
        status['session_id'] = status['run_id']
        status['writes_enabled'] = allow_changes
        return status

    def conversation_change(request):
        """Require what every conversation change needs: SMPA enabled plus the common change rules."""
        smpa_available()
        authorised(request)

    @app.get('/api/v1/smpa/layout')
    def get_smpa_layout():
        """Show where the GUI looks for SMPA, its conversation server and the per-run files."""
        smpa_available()
        return envelope(layout=smpa_layout.describe(layout), views=list(smpa_layout.GRAPH_VIEWS))

    @app.get('/api/v1/smpa/models')
    def get_smpa_models():
        """List the conversation models in SMPA's configuration, with SMPA's own availability when its server runs."""
        smpa_available()
        models, problems = smpa_models.catalogue(layout['config'], layout['model_root'], smpa_root=layout['smpa_root'])
        agents = (conversation.snapshot or {}).get('agents')
        if agents:
            models = smpa_models.merge_server_agents(models, agents)
        return envelope(models=models, problems=problems, config=str(layout['config']),
                        verified_by='conversation_server' if agents else 'gui_precheck')

    @app.get('/api/v1/smpa/conversation')
    def get_conversation():
        """Describe the conversation server and the active conversation (messages included)."""
        smpa_available()
        return envelope(conversation=conversation_status())

    @app.post('/api/v1/smpa/conversation/start')
    def post_conversation_start(body: ConversationStart, request: Request):
        """Start a new conversation run in SMPA with the chosen model, starting SMPA's server if needed."""
        conversation_change(request)
        return conversation_reply(lambda: conversation.start(body.source, body.request_id))

    @app.post('/api/v1/smpa/conversation/resume')
    def post_conversation_resume(body: ConversationResume, request: Request):
        """Reopen an existing conversation session so it can be continued."""
        conversation_change(request)
        directory = session_directory(body.session_id)
        return conversation_reply(lambda: conversation.resume(body.session_id, directory, body.source, body.request_id))

    @app.post('/api/v1/smpa/conversation/model')
    def post_conversation_model(body: ConversationModel, request: Request):
        """Switch the active conversation to another model."""
        conversation_change(request)
        return conversation_reply(lambda: conversation.change_model(body.source))

    @app.post('/api/v1/smpa/conversation/messages')
    def post_conversation_message(body: ConversationMessage, request: Request):
        """Send one message; answers when SMPA has saved it and replied (or explained why it could not)."""
        conversation_change(request)
        if not body.text.strip():
            raise HTTPException(status_code=422, detail='The message is empty')
        return conversation_reply(lambda: conversation.send(body.text, body.request_id))

    @app.post('/api/v1/smpa/conversation/stop')
    def post_conversation_stop(request: Request):
        """Stop the GUI-managed conversation server; SMPA finalizes its run."""
        conversation_change(request)
        return conversation_reply(conversation.stop)

    @app.get('/api/v1/smpa/sessions/{session_id}/conversation')
    def get_session_conversation(session_id: str):
        """Read a session's transcript from the first existing configured location, read-only."""
        directory = session_directory(session_id)
        try:
            path, relative, fallback = smpa_layout.run_file(directory, layout, 'conversation')
        except smpa_layout.LayoutError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return envelope(session_id=session_id, configured_path=relative, candidates=layout['files']['conversation'],
                        fallback=fallback, active=conversation.status(refresh=False)['run_id'] == session_id,
                        **conversation_transcript.read(path))

    @app.get('/api/v1/smpa/graphs/{view}')
    def get_smpa_graph(view: str, source: str = Query('real', pattern='^(real|mock)$'), session_id: str = Query('')):
        """Read one graph view (sense, belief, plan) as nodes and edges, from a session or the mock fixtures.

        Real graphs come from the session's run directory: the first existing
        configured candidate path is used (``fallback`` is true when it is not
        the preferred one). Mock graphs come only from the GUI's mock folder
        and are always marked ``mock``; nothing is ever written.
        """
        smpa_available()
        if view not in smpa_layout.GRAPH_VIEWS:
            raise HTTPException(status_code=404, detail='Unknown graph view')
        if source == 'mock':
            path = Path(layout['mock_graphs']) / f'{view}.ttl'
            configured, candidates, fallback = str(path), [str(path)], False
        else:
            if not session_id:
                raise HTTPException(status_code=422, detail='Choose a session for real graphs')
            candidates = layout['files'][view]
            try:
                path, configured, fallback = smpa_layout.run_file(session_directory(session_id), layout, view)
            except smpa_layout.LayoutError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        return envelope(view=view, source=source, mock=source == 'mock', session_id=session_id or None,
                        configured_path=configured, candidates=candidates, fallback=fallback, **rdf_graphs.read(path))

    app.mount('/', StaticFiles(directory=FRONTEND, html=True), name='frontend')
    return app
