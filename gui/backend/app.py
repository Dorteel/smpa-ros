"""Serve the documented HTTP and WebSocket contract and the static frontend.

Concepts:
    A snapshot endpoint answers one question about the ROS graph as JSON.
    A stream is a WebSocket on which the gateway sends the newest message of
    one topic at a bounded rate, together with status messages that explain
    silence (no publishers, waiting, unsupported data). Opening a stream
    subscribes; closing it unsubscribes. No endpoint changes anything in ROS.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import CONTRACT_VERSION, ros_graph, ros_parameters, ros_topics, sensors
from backend.gateway import Gateway, GatewayError

FRONTEND = Path(__file__).resolve().parent.parent / 'frontend'
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


def create_app(gateway=None):
    """Build the web application around one gateway.

    The gateway joins ROS when the application starts and leaves when it
    stops, so starting and stopping the GUI never depends on other ROS
    processes. A gateway may be supplied by tests; otherwise one is created.
    """
    gateway = gateway or Gateway()

    @asynccontextmanager
    async def lifespan(app):
        """Start the ROS gateway before serving and stop it afterwards."""
        gateway.start()
        try:
            yield
        finally:
            gateway.stop()

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

    app.mount('/', StaticFiles(directory=FRONTEND, html=True), name='frontend')
    return app
