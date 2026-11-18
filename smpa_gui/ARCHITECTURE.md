# GUI architecture

## Principles

1. **Detachable.** The GUI is the ROS 2 package `smpa_gui`: one process with one ROS 2
   node (`/smpa_gui`). It depends on no SMPA package, imports no SMPA module and
   changes no SMPA configuration, launch file or data. Generic ROS 2 functionality lives
   in `smpa_gui.ros_adapter`; the optional SMPA functionality lives in
   `smpa_gui.smpa_adapter` (since version 3: SMPA's recorded run directories, read-only;
   the interface it relies on is listed in SMPA_CONTRACTS.md) and can be switched off.
   Since version 6 the Dialogue starts SMPA's own conversation server as a child process
   (with SMPA's interpreter and configuration) and relays to it over HTTP; the GUI still
   imports nothing from SMPA and implements no inference.
2. **Observational, with confirmed changes.** The gateway creates subscriptions and
   short-lived service clients for inspection. It has no application publishers and no
   service servers. It changes something on the ROS graph only through four `POST`
   routes that are local-only, need an explicit user confirmation in the page and are
   never repeated: sending and cancelling action goals (version 2), setting parameters
   and calling services (version 5). The conversation routes (version 6) follow the same
   rules and act only through SMPA's conversation server. Every other route is `GET` or
   WebSocket.
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
| `ros_adapter/ros_parameters.py` | Read parameters and descriptors (one node, or many side by side for the Workspace); check proposed changes; apply confirmed changes (atomic or individual) once, with a session history |
| `ros_adapter/ros_services.py` | Discover services with providers and clients; describe service types; make confirmed, bounded calls once, with a session history |
| `ros_adapter/sensors.py` | Sensor registry and one encoder per sensor type |
| `ros_adapter/ros_interfaces.py` | Action class lookup; schema of any message type from its definition; validation of JSON values; message construction |
| `ros_adapter/action_goals.py` | Action clients, goal records, cancel, tidy timer, shutdown policy |
| `ros_adapter/ros_clients.py` | Programmatic, awaitable service calls and action goals for the backend's own use (Dialogue, SMPA status), short-lived clients, failure states, topic relays |
| `smpa_adapter/smpa_runs.py` | Optional, read-only adapter for SMPA's memory directory: sessions, activity, normalized observations, confined file access |
| `smpa_adapter/smpa_layout.py` | The configurable paths: SMPA checkout (packages or flat layout), interpreter, configuration, memory, conversation server, per-run file candidates (confined to the run) |
| `smpa_adapter/smpa_models.py` | Conversation model choices from SMPA's effective configuration (`inference_config` overlay) and model registry, with the same selection rule as SMPA and availability prechecks |
| `smpa_adapter/conversation_service.py` | Starts/stops SMPA's `conversation_web` (or uses an external one), relays requests one at a time, deduplicates request ids, keeps the server's output |
| `smpa_adapter/conversation_transcript.py` | Read-only reader of SMPA's length-delimited `conversation.md`, tolerant of a partially written last entry |
| `smpa_adapter/rdf_graphs.py` | Turtle file to generic nodes/edges (rdflib), with file state and content revision |
| `smpa_adapter/smpa_services.py` | Endpoint names and read-only calls of SMPA's existing ROS interfaces: run control (`GetSmpaStatus`, `SmpaEvent`) and inference server (`ListModels`, `GetModelStatus`, `InferenceStatus`) |
| `smpa_adapter/dialogue.py` | Dialogue coordinator: stage adapters (pending or ROS), belief context, turn records, retry, events |

### Process life cycle

`server.main` parses options (cutting off any `--ros-args` that `ros2 run` or launch
append), builds the application and runs uvicorn. uvicorn starts serving and runs the
application's lifespan start: the gateway creates a private rclpy context (without
rclpy's signal handlers, so uvicorn keeps SIGINT/SIGTERM), the node and a
single-threaded executor on its own thread, then the goal manager, parameter writer and
service caller. The lifespan end also stops a conversation server the GUI started. On SIGINT or SIGTERM (Ctrl+C, `ros2 launch` stopping) uvicorn stops
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
| `tabs/ros/` | `graph.js`, `workspace.js` (+ `workspace_canvas.js`), `topics.js`, `parameters.js` (+ `parameter_values.js`, the pure parsing and checking rules), `services.js` (reuses the Actions form), `actions.js` |
| `tabs/sensors/index.js` | Panel manager and sensor discovery list |
| `tabs/sensors/registry.js` | Viewer name → viewer module |
| `tabs/sensors/viewers/` | One module per viewer |
| `tabs/actions/index.js` | Actions tab: server list, editor, confirmation dialog, monitor, history |
| `tabs/actions/form.js` | Schema-driven goal form and client-side validation |
| `tabs/actions/store.js` | Saved goal configurations in browser storage |
| `tabs/smpa/index.js` | SMPA shell: session selector, activity, banners, section registry (Dialogue, Sense, Model, Plan, Act) |
| `tabs/smpa/dialogue.js` | Dialogue section: model selector, conversation, Sense/Belief/Plan graph views |
| `tabs/smpa/rdf_view.js` | Generic interactive RDF drawing (Cytoscape) with incremental updates and node inspector |
| `tabs/smpa/sense.js` | Sense section: See, Hear, Read panels and observation timeline |
| `vendor/cytoscape.min.js` | Cytoscape.js 3.35.0 (MIT), unmodified |

A tab, a ROS 2 subview and an SMPA module share one contract: an object with `id`,
`title`, `mount(container, route)` and `unmount()`. `unmount` must stop polling and
close streams.

## ROS 2 Workspace

The Workspace (`tabs/ros/workspace.js`) is a ROS 2 subview with five CSS-grid regions:
topics (top), services (left), actions (right), parameters (bottom) and the canvas
(centre), separated by splitters. It is independent of SMPA.

- **No new discovery.** The topic, service and action panels poll the same routes as the
  Topics, Services and ROS 2 → Actions views (`/ros/topics`, `/ros/services`,
  `/ros/actions`). Parameters use `GET /ros/parameters`, which only runs the existing
  per-node reader (`ros_parameters.read`) for every visible node side by side
  (`ros_parameters.read_many`).
- **Panels.** Each panel tracks what it has seen (to show disappeared interfaces as
  *gone* for ten seconds), filters locally, and redraws only when what it would show
  changes. A collapsed panel does not poll.
- **Inspection reuses the existing views** through the address: `#ros/topics/<name>`,
  `#ros/services/<name>`, `#actions/<name>` (the Actions tab now selects an action named
  in its address) and `#ros/parameters/<node>@<parameter>` (the Parameters view now
  highlights and scrolls to the named parameter). Parameter changes therefore keep the
  Parameters view's proposal, validation and confirmation dialog unchanged; the
  Workspace itself never posts anything.
- **Canvas** (`workspace_canvas.js`): a Cytoscape.js instance with no elements, user
  panning and zooming on, node grabbing and box selection off. The grid is a CSS
  background moved with the viewport. The viewport is published as `data-zoom`,
  `data-pan-x`, `data-pan-y`, `data-elements` on `#workspace-canvas` (tests read these
  instead of Cytoscape objects) and kept in `sessionStorage`; panel sizes and switches
  in `localStorage`.
- **Prepared for the editor, not implemented.** Each chip carries `data-kind`,
  `data-name`, `data-type` (and `data-node`, `data-value` for parameters), which is
  everything an editor needs to create a canvas element for that interface without
  asking the gateway again. A future drag-and-drop editor can add elements to the
  existing Cytoscape instance; `fit` already frames elements when there are any. Today
  chips are not draggable and nothing is ever added to the canvas.

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
  optional `update(session)`. The Dialogue (versions 4 and 6) was added this way; Model, Plan and
  Act will be too.

## SMPA Dialogue (version 4, integrated into the package in version 6)

```
browser ─► /api/v1/smpa/conversation/* ─► conversation_service.py ─HTTP (loopback, no Origin)─►
          SMPA: python -m smpa_ros.conversation_web --config … --world-model-root … --port 8766 [--resume run]
                (SMPA's .venv, cwd = checkout, PYTHONPATH = smpa_ros:smpa_llm, sourced SMPA workspace;
                 writes runs/<run>/conversation.md, metadata, belief-state.ttl)
browser ─► /api/v1/smpa/graphs/<view> ─► rdf_graphs.py ─read─► runs/<run>/<first existing files.view candidate>
                                                            (or smpa_gui/mock_graphs)
browser ─► /api/v1/smpa/sessions/<id>/conversation ─► conversation_transcript.py ─read─► runs/<run>/<files.conversation>
```

- **Ownership.** Inference, transcripts, run lifecycle and graph contents belong to SMPA.
  The GUI's part is process management, relaying, reading and drawing. It writes no
  transcript and no Turtle file; mock graphs are read from the package's
  `mock_graphs` only. The model catalogue re-implements SMPA's configuration overlay and
  agent selection rule read-only (no SMPA import); once the server runs, SMPA's own agent
  list replaces it. A test compares both against SMPA's real configuration.
- **Why a child process.** SMPA's conversation server is the existing inference and
  persistence path. Starting it with SMPA's own `.venv` avoids importing SMPA (and its
  model stack) into the GUI's process and keeps the GUI detachable. It is started only
  when a conversation is started or reopened, because starting it creates a run.
- **One request at a time.** SMPA's server is single-threaded. The adapter serializes
  operations, reports `busy` with the last known state during a reply, and never polls
  SMPA while a reply is being generated.
- **Paths are configuration.** Every SMPA path, including the per-run transcript and
  graph files, comes from `smpa_layout.py` (defaults, YAML layout file, environment).
  Per-run files are candidate lists: the requested `world_model/…` location first, then
  where SMPA writes today, so moving SMPA's files (SMPA_CONTRACTS.md C6, C7) needs no GUI
  change. Both SMPA checkout layouts (ROS 2 packages and flat) are recognised.
- **Optional.** With `--no-smpa` no conversation service is used and every Dialogue
  route answers that SMPA is disabled; the page shows a note instead of the Dialogue.
- **Graph drawing.** `tabs/smpa/rdf_view.js` merges each new revision into the
  Cytoscape drawing (add/remove/update), lays out only when nodes were added and never
  refits after the first drawing, so the viewport is kept. Malformed or unreadable files
  keep the last valid drawing of that view. Tapping a node shows its IRI, literal values
  and links; tapping an edge shows its triple.
- **Frontend.** `tabs/smpa/dialogue.js` is a section like Sense. It polls the
  conversation status every 1.5 s, the graph every 1.5 s and the models every 15 s.
  Starting a conversation selects the new run in the shell (`chooseSession`).

## Dialogue over ROS 2 (version 8, backend prepared)

The SMPA section formerly called Dashboard is the **Dialogue**. Its backend can now act
as a ROS 2 client of SMPA's independent cognitive nodes:

```
browser ─► POST /api/v1/smpa/dialogue/turns ─► dialogue.DialogueCoordinator (asyncio task)
             Sense ─► Model ─► [belief snapshot read: rdf_graphs on the belief context's run] ─► Plan ─► Act
             each stage: adapter ─► ros_clients.RosClients ─► gateway node (single executor thread)
browser ◄─ WS /api/v1/smpa/dialogue/stream ◄─ coordinator events + relayed /smpa/events, /llm/inference_status
browser ─► GET /api/v1/smpa/ros ─► smpa_services ─► /smpa/get_status, /llm/list_models, /llm/get_model_status
```

- **Order belongs to the Dialogue.** The coordinator calls the four stages one after the
  other and passes values between them. It implements no cognition, inference or belief
  update; Model alone changes beliefs. It holds one turn at a time.
- **Non-blocking.** ROS entities are still created only on the gateway's executor thread.
  `ros_clients.bridge` turns each ROS future into an asyncio future of the web server's
  event loop, so waiting for a long action blocks neither the loop nor other requests.
  Clients exist only during a call; relays only while a stream is open.
- **Adapter boundary.** `PendingStage` (today, for all four stages) is never called and
  says why. `RosStage` calls a configured service or action: the layout's
  `dialogue.stages.<stage>` gives the endpoint, the installed type, a request template
  over the turn values and the result fields that become outputs (`observation_id`,
  `model_invocation_id`, `belief_revision`, `response`, `delivery`). When Codex's stage
  interfaces exist, connecting them is configuration, or one adapter class per stage if a
  contract needs more than templates and field paths. No stage contract is invented.
- **Belief context.** Independent: the Dialogue's session (a run in SMPA's memory) is the
  belief context. Shared: the robot's run, from `GetSmpaStatus` or chosen explicitly.
  The identifiers (`context_mode`, `run_id`, `session_id`, `belief_context_id`) are turn
  values every stage request can carry.
- **Snapshots, not predictions.** The belief graph of the belief context is read before
  Model and again after Model succeeded, with the same reader as the graph view; the
  `belief_snapshot` event carries the revision the graph view will show. Nothing is
  published when Model fails.
- **Association.** The turn record keeps message, observation id, Model invocation id,
  belief snapshot and response together; a retry starts at the failed stage and reuses
  the earlier outputs instead of calling Sense or Model again.
- **Existing behaviour kept.** While any stage is pending the turn route answers 409 and
  the page keeps using SMPA's conversation server; the page only shows which path
  answers. No LLM setting is duplicated: model settings stay ROS parameters of
  `smpa_llm`'s `llm` node.

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
- Version 6 merges the version 4 Dialogue into the package; the report had no
  conversational interface.
- Version 5 turns `gui/` into the ROS 2 package `smpa_gui` (the report had planned a
  standalone folder outside colcon) and adds parameter changes and service calls, which
  the report had excluded. The access-control and confirmation model of version 2 was
  reused unchanged for both.
