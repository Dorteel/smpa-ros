# GUI architecture

## Principles

1. **Detachable.** The GUI is one process with one ROS 2 node (`/smpa_gui`). It imports
   no SMPA module, reads no SMPA file, and changes no SMPA configuration.
2. **Observational.** The gateway creates subscriptions and parameter-service clients
   only. It has no application publishers, no service servers, sends no action goals
   and writes no parameters. The HTTP application defines only `GET` and WebSocket routes.
3. **Everything ROS stays behind the gateway.** The frontend knows only the contract in
   [API.md](API.md). Another frontend (for example TouchDesigner) can use the same contract.
4. **Work only for what is visible.** Discovery is polled only by the visible view. A
   topic is subscribed only while a stream for it is open.

## Processes and threads

```
browser  ──HTTP/WebSocket──►  uvicorn + FastAPI (asyncio thread, worker threads)
                                   │  gateway.call(function)
                                   ▼
                              ROS thread: rclpy SingleThreadedExecutor
                              node /smpa_gui  ──DDS──►  any ROS 2 graph
```

All rclpy calls happen on the ROS thread. Web handlers pass small functions to
`Gateway.call`, which runs them as executor tasks and returns the result or raises a
`GatewayError` after a timeout.

## Backend modules (`gui/backend/`)

| Module | Responsibility |
|---|---|
| `gateway.py` | The node, its thread, `call`, a one-second discovery cache, and shared subscriptions ("slots") with viewer counting |
| `ros_names.py` | Name rules: full node names, hidden names, action plumbing names |
| `ros_graph.py` | Collect raw discovery; build the graph description (pure function) |
| `ros_topics.py` | Message class lookup, QoS matching for subscriptions, bounded message-to-JSON conversion |
| `ros_parameters.py` | Read another node's parameters with bounded waiting |
| `sensors.py` | Sensor registry and one encoder per sensor type |
| `app.py` | Routes, the generic stream loop, static files |
| `__main__.py` | Command-line entry point |

### Subscriptions

`Gateway.acquire(topic, type)` returns a slot. Viewers of the same topic and type share
one subscription. The slot keeps only the newest message. `Gateway.release` destroys the
subscription when the last viewer leaves. The subscription QoS is best-effort and
volatile unless every publisher is reliable or transient-local, so it is compatible with
any publisher and still receives latched messages.

### Streams and rate limiting

One loop serves every stream. At the stream's rate (default per sensor type, at most 30
per second) it checks whether a newer message exists, encodes it on a worker thread and
sends it. Messages arriving faster than the rate are dropped at the slot, so a slow
browser never causes a queue. Silence is explained with status messages.

## Frontend modules (`gui/frontend/`)

| File | Responsibility |
|---|---|
| `app.js` | Tab shell, routing by address fragment, health indicator |
| `api.js` | The only code that talks to the gateway: snapshots, streams with reconnect, polling |
| `tabs/ros/` | `graph.js`, `topics.js`, `parameters.js`, `actions.js` |
| `tabs/sensors/index.js` | Panel manager and sensor discovery list |
| `tabs/sensors/registry.js` | Viewer name → viewer module |
| `tabs/sensors/viewers/` | One module per viewer |
| `tabs/smpa/index.js` | Empty module registry |
| `vendor/cytoscape.min.js` | Cytoscape.js 3.35.0 (MIT), unmodified |

A tab, a ROS 2 subview and an SMPA module share one contract: an object with `id`,
`title`, `mount(container, route)` and `unmount()`. `unmount` must stop polling and
close streams.

## Sensor registry and adding a sensor type

The gateway registry (`sensors.REGISTRY`) maps a ROS message type to a viewer name, a
default rate and an encoder. The frontend registry maps the viewer name to a module
exporting `create(container)`, which returns `{update(header, binary), reset(), destroy()}`.

To support a new sensor type:

1. Add an encoder function and one `REGISTRY` entry in `backend/sensors.py`.
2. Add a viewer module in `frontend/tabs/sensors/viewers/` and one entry in
   `frontend/tabs/sensors/registry.js` (or reuse an existing viewer name).

Nothing else changes. Types without a registry entry stay inspectable in ROS 2 → Topics.

## Adding an SMPA module later

Add a module object to `MODULES` in `frontend/tabs/smpa/index.js`, and backend routes
under `/api/v1/smpa/…` in a new backend module. How such a module obtains SMPA state
(ROS interfaces, run files, or the conversation server) is deliberately undecided and
needs an agreed interface with the SMPA core.

## Deviations from GUI_ARCHITECTURE_REPORT.md

- The image viewer lives in the Sensors tab, as later requested.
- The graph uses snapshot polling instead of a WebSocket difference stream.
- Actions are derived from the graph snapshot, so there is no separate `ros_actions.py`.
- NumPy is used (it is part of every ROS 2 Python installation) but is not installed by the GUI.
- `install_gui.sh` was added so that `run_gui.sh` never installs anything.
