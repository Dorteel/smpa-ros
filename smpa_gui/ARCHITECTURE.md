# GUI architecture

## Principles

1. **Detachable.** The GUI is the ROS 2 package `smpa_gui`: one process with one ROS 2
   node (`/smpa_gui`). It depends on no SMPA package, imports no SMPA module and
   changes no SMPA configuration, launch file or data. Generic ROS 2 functionality lives
   in `smpa_gui.ros_adapter`; the optional SMPA functionality lives in
   `smpa_gui.smpa_adapter` (since version 3: SMPA's recorded run directories, read-only;
   the interface it relies on is listed in SMPA_CONTRACTS.md) and can be switched off.
2. **Observational, with confirmed changes.** The gateway creates subscriptions and
   short-lived service clients for inspection. It has no application publishers and no
   service servers. It changes something on the ROS graph only through four `POST`
   routes that are local-only, need an explicit user confirmation in the page and are
   never repeated: sending and cancelling action goals (version 2), setting parameters
   and calling services (version 5). Every other route is `GET` or WebSocket.
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

## Package (`smpa_gui/`)

| Path | Responsibility |
|---|---|
| `package.xml`, `setup.py`, `setup.cfg`, `resource/smpa_gui` | ament_python package: Python package plus frontend as package data, console script `smpa_gui` in `lib/smpa_gui`, manifest and launch file in `share/smpa_gui`, ament index marker |
| `launch/gui.launch.py` | Declares `host`, `port`, `read_only`, `keep_goals_on_exit`, `smpa`, `smpa_memory_root` and starts the one GUI process (`launch_ros` `Node`) |
| `install_gui.sh` | Creates `smpa_gui/.venv` (pinned web packages, ROS packages via system site-packages); `--build WS` colcon-builds the package with that interpreter |
| `run_gui.sh` | Runs the package from its source folder without building (sources ROS and an overlay) |

## Backend modules (`smpa_gui/smpa_gui/`)

| Module | Responsibility |
|---|---|
| `server.py` | Entry point (`ros2 run smpa_gui smpa_gui`, `python -m smpa_gui`): options, ROS-argument stripping, uvicorn |
| `app.py` | Routes, access rules, the generic stream loop, static files; ROS start-up and shutdown in the lifespan |
| `ros_adapter/gateway.py` | The node, its thread, `call`, a one-second discovery cache, and shared subscriptions ("slots") with viewer counting |
| `ros_adapter/ros_names.py` | Name rules: full node names, hidden names, action plumbing names |
| `ros_adapter/ros_graph.py` | Collect raw discovery; build the graph description (pure function) |
| `ros_adapter/ros_topics.py` | Message class lookup, QoS matching for subscriptions, bounded message-to-JSON conversion |
| `ros_adapter/ros_parameters.py` | Read parameters and descriptors; check proposed changes; apply confirmed changes (atomic or individual) once, with a session history |
| `ros_adapter/ros_services.py` | Discover services with providers and clients; describe service types; make confirmed, bounded calls once, with a session history |
| `ros_adapter/sensors.py` | Sensor registry and one encoder per sensor type |
| `ros_adapter/ros_interfaces.py` | Action class lookup; schema of any message type from its definition; validation of JSON values; message construction |
| `ros_adapter/action_goals.py` | Action clients, goal records, cancel, tidy timer, shutdown policy |
| `smpa_adapter/smpa_runs.py` | Optional, read-only adapter for SMPA's memory directory: sessions, activity, normalized observations, confined file access |

### Process life cycle

`server.main` parses options (cutting off any `--ros-args` that `ros2 run` or launch
append), builds the application and runs uvicorn. uvicorn starts serving and runs the
application's lifespan start: the gateway creates a private rclpy context (without
rclpy's signal handlers, so uvicorn keeps SIGINT/SIGTERM), the node and a
single-threaded executor on its own thread, then the goal manager, parameter writer and
service caller. On SIGINT or SIGTERM (Ctrl+C, `ros2 launch` stopping) uvicorn stops
accepting requests and runs the lifespan end: active goals are settled, the node and
context are destroyed, and the process exits with status 0. The launch file gives the
process 15 s before SIGTERM and 5 s more before SIGKILL.

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

## Frontend modules (`smpa_gui/smpa_gui/frontend/`)

| File | Responsibility |
|---|---|
| `app.js` | Tab shell, routing by address fragment, health indicator |
| `api.js` | The only code that talks to the gateway: snapshots, streams with reconnect, polling |
| `tabs/ros/` | `graph.js`, `topics.js`, `parameters.js` (+ `parameter_values.js`, the pure parsing and checking rules), `services.js` (reuses the Actions form), `actions.js` |
| `tabs/sensors/index.js` | Panel manager and sensor discovery list |
| `tabs/sensors/registry.js` | Viewer name → viewer module |
| `tabs/sensors/viewers/` | One module per viewer |
| `tabs/actions/index.js` | Actions tab: server list, editor, confirmation dialog, monitor, history |
| `tabs/actions/form.js` | Schema-driven goal form and client-side validation |
| `tabs/actions/store.js` | Saved goal configurations in browser storage |
| `tabs/smpa/index.js` | SMPA shell: session selector, activity, banners, section registry (Sense, Model, Plan, Act) |
| `tabs/smpa/sense.js` | Sense section: See, Hear, Read panels and observation timeline |
| `vendor/cytoscape.min.js` | Cytoscape.js 3.35.0 (MIT), unmodified |

A tab, a ROS 2 subview and an SMPA module share one contract: an object with `id`,
`title`, `mount(container, route)` and `unmount()`. `unmount` must stop polling and
close streams.

## Action execution

```
click "Send Goal…" → validate in page → confirmation dialog → click "Send goal now"
   → POST /actions/goals {action, type, goal, request_id} + session token   (once, never retried)
      → access rules → request_id seen before? return the first record
      → resolve action type → validate against the Goal definition → build message
      → create action client → wait ≤2 s for the server → send with a UUID chosen by the gateway
   ← goal record
page polls GET /actions/goals twice per second → state, feedback, result, history
```

- **Records live in the gateway**, not in the browser. A page reload or a lost connection
  only interrupts the display. The browser stores nothing that could cause a resend.
- **Idempotency.** Each confirmation creates one request identifier; the gateway sends a
  given identifier at most once, also under concurrent duplicates.
- **Access control** is described in [API.md](API.md): loopback connection, local `Host`
  and `Origin`, JSON body, per-process session token, optional `--read-only`.
- **Footprint.** An action client exists only while one of its goals is active or being
  submitted. A one-second timer on the ROS thread removes idle clients, refreshes states
  and marks goals as `lost` when their server disappears.
- **Shutdown.** On a normal stop the gateway requests cancellation of its active goals
  and waits up to two seconds for the answers (`--keep-goals-on-exit` disables this).
- **Interfaces are discovered, never hard-coded.** Schemas come from
  `rosidl_runtime_py` and `rosidl_parser` definitions of whatever action type the ROS
  graph reports. No SMPA action, type or name appears in the code.

## Parameter changes and service calls

```
Parameters: Edit → proposal checked in page (parameter_values.js) → "Apply changes…" dialog → "Apply now"
   → POST /ros/parameters/set {node, changes, atomic, request_id} + token          (once, never retried)
      → access rules → request_id seen? return first record
      → read parameters + descriptors afresh → check every change (ros_parameters.check_change) → 422 on any problem
      → set_parameters_atomically | set_parameters (one client, bounded wait) → read back
   ← change record (outcome applied | rejected | partial | unknown | unavailable, per-parameter answers)

Services: select → GET /ros/services/interface → generated form / JSON → "Call service…" dialog → "Call now"
   → POST /ros/services/call {service, type, request, request_id, timeout} + token   (once, never retried)
      → access rules → request_id seen? return first record → validate against Request → build
      → create client → wait ≤2 s for a provider (else 409, nothing sent) → call_async
      → wait on the web worker thread (never the ROS thread) until response | timeout | provider gone
   ← call record (succeeded | timed_out | server_lost | failed); client destroyed
```

- **Same safety model as goals**: confirmation dialog with the exact values, one request
  id per confirmation, records kept in the gateway, local-only access with the session
  token, all disabled in read-only mode, no automatic retries.
- **Checks are advisory, the node decides.** The page and the gateway apply the same
  rules (type, dynamic typing, read-only, range and step); the node's own callbacks have
  the final word and their reasons are shown. Type-valid does not mean physically safe;
  the panels say so.
- **Footprint.** The GUI has no parameter services of its own and holds a service client
  only during a call. Discovery of service providers and clients is cached for one
  second like the graph.
- **Interfaces are discovered, never hard-coded.** Service request and response schemas
  come from the installed definitions, as for actions. No SMPA service appears in the code.

## Sensor registry and adding a sensor type

The gateway registry (`sensors.REGISTRY`) maps a ROS message type to a viewer name, a
default rate and an encoder. The frontend registry maps the viewer name to a module
exporting `create(container)`, which returns `{update(header, binary), reset(), destroy()}`.

To support a new sensor type:

1. Add an encoder function and one `REGISTRY` entry in `smpa_gui/ros_adapter/sensors.py`.
2. Add a viewer module in `frontend/tabs/sensors/viewers/` and one entry in
   `frontend/tabs/sensors/registry.js` (or reuse an existing viewer name).

Nothing else changes. Types without a registry entry stay inspectable in ROS 2 → Topics.

## SMPA tab (optional)

SMPA integration can be disabled (`--no-smpa`, `smpa:=false`): the SMPA routes then
answer that it is disabled and nothing is read. No ROS functionality depends on it.

```
SMPA memory/runs/*  ──read only──►  smpa_runs.py  ──►  /api/v1/smpa/*  ──►  tabs/smpa/
ROS graph (names under /run_<uuid>) ──────────────┘ (activity)
ROS camera topic, Listen status topic ──► existing sensor / echo streams ──► LIVE areas
```

- **Recorded versus live.** Recorded observations come only from SMPA's files and are
  labelled RECORDED BY SMPA. Live data comes only from ROS streams and is labelled LIVE;
  the GUI never stores it.
- **Normalization.** The adapter turns SMPA's several file layouts into one observation
  shape (API.md). Every time stamp carries its source; fields SMPA did not record are null.
- **Refresh.** The page polls the session list every 3 s and a running session's
  observations every 2 s; a per-session `revision` (file names, sizes, times) avoids
  redrawing unchanged data. Ended sessions are rechecked every 15 s.
- **Safety.** Files are opened read-only, no locks are taken (SMPA's conversation server
  locks its transcript), file downloads are confined to recorded media inside a session.
- **Sections.** A section is a module with `mount(container, session)`, `unmount()` and an
  optional `update(session)`. Model, Plan and Act will be added the same way.

## Deviations from GUI_ARCHITECTURE_REPORT.md

- The image viewer lives in the Sensors tab, as later requested.
- The graph uses snapshot polling instead of a WebSocket difference stream.
- Actions are derived from the graph snapshot, so there is no separate `ros_actions.py`.
- NumPy is used (it is part of every ROS 2 Python installation) but is not installed by the GUI.
- `install_gui.sh` was added so that `run_gui.sh` never installs anything.
- Version 3 reads SMPA's run directories for the SMPA tab; the report had left the SMPA
  data source undecided.
- Version 2 adds action execution, which the report had excluded from version 1.
  Goal monitoring uses polling of gateway-held records instead of a WebSocket, so that
  a broken connection can never be mistaken for, or cause, a resend.
- Version 5 turns `gui/` into the ROS 2 package `smpa_gui` (the report had planned a
  standalone folder outside colcon) and adds parameter changes and service calls, which
  the report had excluded. The access-control and confirmation model of version 2 was
  reused unchanged for both.
