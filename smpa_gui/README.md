# smpa_gui: SMPA-ROS GUI as a ROS 2 package

A standalone, browser-based GUI for ROS 2, packaged as the `ament_python` package
`smpa_gui`. It runs as its own process and its own ROS 2 node (`/smpa_gui`), depends on
no SMPA package, and works whether or not SMPA is running. It observes the ROS graph and
acts on it only when a user on this computer explicitly confirms each action: sending
and cancelling action goals, changing parameters, and calling services. Its optional
SMPA tab holds a conversational Dashboard that uses SMPA's own conversation server
(started as a separate process); the GUI implements no inference of its own.

| Tab | Content |
|---|---|
| **ROS 2** | Graph (rqt_graph-like), Topics (types, endpoints, QoS, live messages), **Parameters** (read, propose, confirm and apply changes), **Services** (discover, inspect, confirm and call), Actions (passive list) |
| **Sensors** | Live viewers for nine `sensor_msgs` types in freely arranged panels |
| **Actions** | Discover action servers, edit a goal in a generated form or as JSON, send and cancel goals, watch feedback, result and history |
| **SMPA** (optional) | Session selector and five sections. **Dashboard** (default): a conversation with SMPA (model selector, chat, saved and reopenable conversations) beside interactive Sense, Belief and Plan graphs read from Turtle files. **Sense**: See, Hear and Read panels and a unified observation timeline from SMPA's recorded sessions, plus live camera and Listen state. Model, Plan and Act are placeholders |

Further documentation: [ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md),
[SMPA_CONTRACTS.md](SMPA_CONTRACTS.md), [VERIFICATION.md](VERIFICATION.md),
[MIGRATION.md](MIGRATION.md) (from `gui/` to `smpa_gui/`, and from GUI v5 to v6).

## Package layout

```
smpa_gui/                      the ROS 2 package (was gui/ up to GUI version 3)
├── package.xml, setup.py, setup.cfg, resource/smpa_gui
├── launch/gui.launch.py       ros2 launch smpa_gui gui.launch.py
├── smpa_gui/                  Python package
│   ├── server.py              console entry point `smpa_gui` (also python -m smpa_gui)
│   ├── app.py                 HTTP/WebSocket routes, access rules, static frontend
│   ├── ros_adapter/           generic ROS 2: gateway node, graph, topics, sensors,
│   │                          parameters, services, interfaces, action goals
│   ├── smpa_adapter/          optional, read-only SMPA run directories
│   └── frontend/              browser assets, installed as package data
├── test/                      unit, ROS integration, packaging and browser tests
└── install_gui.sh, run_gui.sh, test_gui.sh
```

## Requirements

- Linux with ROS 2 (developed for Jazzy) providing `rclpy`, `rosidl_runtime_py`,
  `rcl_interfaces`, NumPy and Pillow, plus `colcon` for the package build
  (`sudo apt install python3-colcon-common-extensions`).
- Python 3.12 with the `venv` module (`sudo apt install python3-venv` on Ubuntu).
- A current browser with WebGL (needed only for the 3D point cloud viewer).

### Python environment

The GUI needs three web packages that ROS does not provide: FastAPI, uvicorn and
websockets. The tested versions are pinned in `requirements.txt` and installed into
**one** virtual environment, `smpa_gui/.venv`, created with access to the ROS Python
packages (`--system-site-packages`). SMPA's `.venv` is not used and nothing is
installed system-wide.

Building the package with that environment's interpreter (as `install_gui.sh --build`
does, and as SMPA's own `install.sh` does for SMPA) makes the installed `smpa_gui`
console script start with it, so `ros2 run` and `ros2 launch` need no further setup.
`package.xml` also declares the rosdep keys `python3-fastapi`, `python3-uvicorn` and
`python3-websockets`; the Ubuntu 24.04 versions of those are older than the pinned ones
and were **not tested** (FastAPI must be new enough to use Pydantic 2).

## Install and build

```bash
cd ~/ros2_ws/src/smpa-ros/smpa_gui
./install_gui.sh                   # creates smpa_gui/.venv and installs requirements.txt
./install_gui.sh --dev             # additionally what the tests need
./install_gui.sh --build ~/ros2_ws # also colcon-build smpa_gui into ~/ros2_ws/install
```

`--build` runs exactly:

```bash
cd ~/ros2_ws
~/ros2_ws/src/smpa-ros/smpa_gui/.venv/bin/python "$(command -v colcon)" build \
    --paths ~/ros2_ws/src/smpa-ros/smpa_gui --packages-select smpa_gui
```

SMPA's workspace builds packages by explicit `--paths` (see `install.sh` and `test.sh`
in the repository root), so `smpa_gui` is built only when named. The scripts use the
ROS 2 environment that is already sourced, or source `/opt/ros/$ROS_DISTRO/setup.bash`
(default `jazzy`); set `SMPA_GUI_ROS_SETUP` for another `setup.bash`.

## Run

As a ROS 2 package (after `--build`):

```bash
source ~/ros2_ws/install/setup.bash
ros2 launch smpa_gui gui.launch.py                      # then open http://127.0.0.1:8765
ros2 launch smpa_gui gui.launch.py port:=9000 read_only:=true smpa:=false
ros2 launch smpa_gui gui.launch.py smpa_root:=$HOME/ros2_ws/src/smpa-ros   # Dashboard with SMPA's conversation server
ros2 run smpa_gui smpa_gui --port 9000 --no-smpa         # the same process without launch
```

From the source folder, without building (the launcher of earlier versions):

```bash
./run_gui.sh                       # then open http://127.0.0.1:8765
./run_gui.sh --read-only --no-smpa
```

Stop it with Ctrl+C (or by stopping the launch). On a normal stop the GUI settles its
action goals (see below) and then leaves the ROS graph; the console says
`SMPA-ROS GUI stopped: goals settled, node left the ROS graph.` Starting and stopping
the GUI does not affect any other ROS process.

### Launch arguments and options

| Launch argument | Command-line option | Environment | Meaning | Default |
|---|---|---|---|---|
| `host` | `--host` | `SMPA_GUI_HOST` | Listening address | `127.0.0.1` |
| `port` | `--port` | `SMPA_GUI_PORT` | Listening port | `8765` |
| `read_only` | `--read-only` | `SMPA_GUI_READ_ONLY=1` | Disable action goals, parameter changes, service calls and conversation changes | `false` |
| `keep_goals_on_exit` | `--keep-goals-on-exit` | `SMPA_GUI_KEEP_GOALS_ON_EXIT=1` | Do not cancel active goals when the GUI stops | `false` |
| `smpa` | `--no-smpa` (when `false`) | `SMPA_GUI_NO_SMPA=1` | Optional SMPA integration | `true` |
| `smpa_memory_root` | `--memory-root` | `SMPA_GUI_MEMORY_ROOT` | SMPA's `memory/` directory for the SMPA tab (read-only) | `<smpa_root>/memory` |
| `smpa_root` | `--smpa-root` | `SMPA_GUI_SMPA_ROOT` | SMPA checkout whose conversation server the Dashboard starts | the repository containing this source checkout, if it holds SMPA; none when installed |
| `smpa_layout` | `--layout` | `SMPA_GUI_LAYOUT` | YAML file with further Dashboard paths ([below](#where-the-dashboard-finds-smpa)) | none |

`run_gui.sh` additionally reads `SMPA_GUI_ROS_SETUP` (ROS 2 `setup.bash` to source) and
`SMPA_GUI_OVERLAY` (a workspace `install/setup.bash` with custom message types, or
`none`; default: the enclosing colcon workspace). With `ros2 launch`, source that
workspace yourself. Topics, services and actions whose types are not available in the
GUI's environment are still listed, but cannot be displayed, edited or called.

Binding `host` to another interface gives that network read access to every topic.
Changes (goals, parameters, service calls) stay restricted to this computer in any case.

### Including the GUI in another launch file

The GUI's launch description starts only the GUI process, so a combined launcher can
include it without modification. For example, a future SMPA launcher could add:

```python
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.substitutions import FindPackageShare
from launch.substitutions import PathJoinSubstitution

gui = IncludeLaunchDescription(
    PythonLaunchDescriptionSource(PathJoinSubstitution([FindPackageShare('smpa_gui'), 'launch', 'gui.launch.py'])),
    launch_arguments={'read_only': 'false', 'smpa': 'true',
                      'smpa_memory_root': '/home/dorteel/ros2_ws/src/smpa-ros/memory'}.items())
```

The GUI needs nothing from the other processes and they need nothing from it; it can
be started before, after or without them. No SMPA launch file was changed.

## Using it

- **Graph**: drag to pan, wheel to zoom, drag elements to rearrange. Search dims
  everything that does not match. The check boxes hide or show topics, actions,
  unconnected topics, `/rosout` and `/parameter_events`, hidden names, and the GUI's
  own node. The graph refreshes every two seconds without changing zoom or pan.
- **Topics**: select a topic to see its publishers and subscribers with QoS. *Start
  echo* subscribes and shows live messages; *Stop echo* unsubscribes.
- **Parameters**: select a node to read its parameters with types, values,
  descriptions and constraints; see [Changing parameters](#changing-parameters-ros-2--parameters).
- **Services**: see [Calling services](#calling-services-ros-2--services).
- **ROS 2 → Actions**: passive list of actions with their types, servers and clients.
- **Sensors**: *Open* a discovered topic, or add an empty panel and choose a topic.
  Any number of panels can be open. A panel subscribes only while it is open and the
  Sensors tab is shown. In the 3D viewer: drag rotates, Shift-drag or right-drag pans,
  wheel zooms, double click re-centres.

## Sending action goals (Actions tab)

1. Select an action server. The goal editor is generated from the action's real Goal
   definition: text, numbers, check boxes, lists with add/remove, nested messages.
   *JSON* shows the same values as editable JSON. Fields you leave out keep the
   defaults of the definition.
2. Optionally save the values under a name (*Saved configurations*, stored in this
   browser, per action type) or load saved values. Loading never sends anything.
3. *Send Goal…* checks the values against the field types and opens a confirmation
   naming the action, the server node and the exact values. Only *Send goal now* in
   that dialog sends the goal. One confirmation sends one goal.
4. The right-hand side shows the selected goal: state, UUID, times, live feedback and
   the result. *Cancel Goal* asks the server to cancel the selected active goal. The
   history lists every goal sent since the GUI server started.

**Every action is treated as potentially physical.** The GUI cannot know whether an
action server moves a robot, so the warning and the confirmation appear for all of them.

Safeguards:

- Nothing is sent by selecting, editing, loading, reloading the page, navigating, or
  reconnecting. A failed request is never retried automatically.
- Each confirmation carries a one-time request identifier; the gateway refuses to send
  the same identifier twice.
- Goals are accepted only from this computer (loopback connection, local `Host` and
  `Origin`), only with the session token of the running gateway, and not at all in
  `--read-only` mode. The token changes whenever the gateway restarts.
- See also [Safeguards for every change](#safeguards-for-every-change).

### What happens to active goals

| Event | Effect on goals that are still active |
|---|---|
| Browser tab closed, reloaded, or network to the GUI server lost | Nothing. Goals keep running; the gateway keeps recording feedback and results. Reopen the page to see them. Nothing is resent. |
| GUI server stopped normally (Ctrl+C) | The gateway asks each action server to cancel its active goals and waits up to two seconds for the answers, then exits. A server may refuse or take time to stop. With `--keep-goals-on-exit` nothing is cancelled and the goals continue on their servers. |
| GUI server killed or crashed | No cancel request is sent. Goals continue on their servers until they finish or another client cancels them. |
| GUI server restarted | The history starts empty and earlier goals cannot be monitored or cancelled from the GUI any more (use `ros2 action` tools). Pages still open from before must be reloaded before they can send again. |
| Action server disappears during a goal | The goal is marked `lost`: its outcome is unknown. |

## Changing parameters (ROS 2 → Parameters)

1. Select a node. Its parameters are read through the node's standard parameter
   services: name, type, current value, description and constraints (read-only,
   "type may change" for dynamic typing, integer or floating-point range with step,
   the node's free-text constraints). *Filter parameters…* narrows the table;
   *Refresh* reads again. A node that does not answer in time is reported as such.
2. *Edit* a writable parameter to propose a value. The input fits the type: true/false
   for booleans, numbers, text, and JSON lists (`[1, 2]`, `["a", "b"]`) for arrays and
   byte arrays. A parameter with dynamic typing also offers a type choice. Every
   proposal is checked as you type against the type, the range and its step; problems
   are shown beside it and block *Apply*. Read-only parameters cannot be edited.
3. *Apply changes…* opens a confirmation listing every parameter with its current and
   proposed value. With *Apply atomically* (the default) the node applies all or none
   (`set_parameters_atomically`); otherwise each change is applied on its own
   (`set_parameters`) and some may succeed while others are rejected.
4. The result shows the node's answer per parameter: applied, or rejected with the
   node's reason. *Partial* means some were applied. If the node does not answer in
   time the outcome is *unknown* (it may have applied them); nothing is retried. The
   values are read back afterwards. Applied proposals disappear; rejected ones stay
   for correction.

The gateway repeats every check with the node's descriptors read at that moment, and
the node has the final word (its own callbacks may reject a value).

## Calling services (ROS 2 → Services)

1. The list shows every discovered service with its type and provider nodes. Parameter
   services, action plumbing, type-description services and hidden names are hidden by
   default; check boxes show them. *Filter services…* searches names and types.
2. Select a service. Its providers and clients are shown, and a request editor is
   generated from the service's real Request definition (nested messages and arrays
   included), with a *JSON* mode as for action goals. The Response definition is listed.
3. *Call service…* checks the request against the field types and opens a confirmation
   showing the service, its providers, the timeout (0.5–60 s, default 10) and the
   complete request. Only *Call now* calls it, once.
4. The response is shown when it arrives. Without a response in time the call is
   *timed out*; if the provider leaves the ROS graph while the call waits it is
   *provider disappeared*. In both cases the outcome is unknown and the call is not
   repeated. The history lists every call made since the GUI server started; *Load
   this request into the editor* reuses a request without calling.

A service without a provider is refused at once (after up to two seconds of discovery)
and nothing is sent. The GUI holds a service client only while one of its calls runs.

## Safeguards for every change

Action goals, parameter changes, service calls and conversation changes (start, message,
reopen, model switch, stop) share the same rules:

- **Explicit confirmation.** Each change is made only by the confirmation button of a
  dialog that shows exactly what will be sent. Nothing is changed by selecting,
  editing, loading, reloading the page, navigating, or reconnecting.
- **Never twice.** Each confirmation carries a one-time request id; the gateway refuses
  to act on the same id twice, so double clicks and reloads cannot repeat a change.
  A failed or unanswered request is never retried.
- **Local only.** Changes are accepted only from this computer (loopback connection,
  local `Host` and `Origin` headers, which also defeats DNS rebinding), only as JSON,
  only with the session token of the running gateway (`GET /api/v1/session`), and not
  at all in read-only mode. The token changes whenever the gateway restarts.
- **Physical effects.** The GUI cannot know whether an action server, a parameter or a
  service controls hardware. Every panel and dialog warns that a robot may move, and a
  value that fits the type (and range) is **not** thereby safe.
- **No other powers.** The gateway does not publish to topics and offers no access to
  files or a shell; the SMPA tab reads recorded files only.

## SMPA tab: Dashboard (conversation and graphs)

The Dashboard is the SMPA tab's first and default section (navigation: Dashboard |
Sense | Model | Plan | Act). Its left column is a conversation with SMPA, its right
column shows the selected session's Sense, Belief and Plan graphs.

### Where the Dashboard finds SMPA

The Dashboard runs SMPA's own conversation server, `python -m smpa_ros.conversation_web`,
with SMPA's own `.venv`, configuration, model folder and memory directory, in SMPA's
checkout as working directory. It needs:

- the SMPA checkout (`smpa_root`): found automatically when this GUI is a source
  checkout inside it (`<smpa-ros>/smpa_gui`); otherwise, and always for an installed
  package, give it: `ros2 launch smpa_gui gui.launch.py smpa_root:=$HOME/ros2_ws/src/smpa-ros`;
- SMPA's `.venv` in that checkout (created by SMPA's `install.sh`);
- the **SMPA workspace sourced** before starting the GUI
  (`source ~/ros2_ws/install/setup.bash`): SMPA's configuration
  `smpa_ros/config.yaml` takes its provider and model defaults from the installed
  `smpa_llm` package (`inference_config: package://smpa_llm/config.yaml`), and SMPA finds
  that package and its model registry only through the sourced workspace.

When something is missing, the Dashboard lists it instead of starting anything. Both
SMPA checkout layouts are recognised: the ROS 2 packages layout
(`smpa_ros/smpa_ros/conversation_web.py`, `smpa_llm/`) and the earlier flat layout.

Everything can also be set in a layout file (`smpa_layout:=` / `--layout`); every entry
is optional, and relative paths are relative to the layout file's folder:

```yaml
smpa_root: ~/ros2_ws/src/smpa-ros
config: ~/ros2_ws/src/smpa-ros/smpa_ros/config.yaml   # default: <root>/smpa_ros/config.yaml
python: ~/ros2_ws/src/smpa-ros/.venv/bin/python       # default: <root>/.venv/bin/python
model_root: ~/ros2_ws/src/smpa-ros/models             # default: <root>/models
memory_root: ~/ros2_ws/src/smpa-ros/memory            # default: <root>/memory
conversation_port: 8766                               # loopback port of the server the GUI starts
conversation_url: null                                # e.g. http://127.0.0.1:8000 to use a running server
mock_graphs: /path/to/other/mock/graphs               # default: the mock_graphs installed with the GUI
files:                    # per-run files, relative to the run directory; first existing candidate wins
  conversation: [world_model/conversation.md, conversation.md]
  sense: [world_model/sense.ttl]
  belief: [world_model/belief.ttl, belief-state.ttl]
  plan: [world_model/plan.ttl]
```

The `files` entries above are the defaults. The first candidate of each is the target
structure `memory/runs/<run>/world_model/{conversation.md,sense,belief,plan.ttl}`; the
second is where SMPA writes today (`conversation.md` and `belief-state.ttl` at the run
root). The Dashboard says when it shows such a fallback location. SMPA does not write
`sense.ttl` or `plan.ttl` yet; those views say "no file yet" until it does
(SMPA_CONTRACTS.md, C6 and C7). `layouts/smpa-current.yaml` (installed in
`share/smpa_gui/layouts`) is an example that uses only today's locations.

### Conversation (left)

- **Model**: every conversation model SMPA is configured with: local text models of the
  model registry (backends `transformers`, `ollama`) and API providers with a text model.
  Each shows its provider, a description built from the configuration (the registry has
  no description field) and its availability: a precheck (weights folder present,
  credential variable set) until SMPA's server runs, then SMPA's own verdict. Listing
  models starts nothing.
- **Start new conversation**: starts SMPA's conversation server if it is not running
  (that is what creates the run; it is never started just to list models) or asks it
  for a new run, then selects the model. The new run becomes the selected session.
- **Chat**: Enter sends, Shift+Enter starts a new line. While SMPA generates, the status
  line says so with the elapsed time and the message is shown as *sending…*. SMPA saves
  the message and the reply in the run's transcript after each exchange; nothing else
  is needed to persist a conversation. If inference fails, SMPA keeps the message and
  the Dashboard shows SMPA's explanation; no other model is tried.
- **Reopen**: select an earlier conversation session. It is shown from its recorded
  transcript (read-only). *Continue this conversation* reopens it in SMPA, which appends
  to the same transcript and restores the model recorded in that run; switch models
  explicitly with *Use … from now on*. SMPA refuses to reopen a run made with a
  different configuration.
- **Stop server**: SMPA finalizes the run. The server is also stopped when the GUI stops.
  Its output is shown under *Server output*.

### Graphs (right)

*Sense*, *Belief* and *Plan* each read one Turtle file of the selected session. Every
resource is a node and every resource-to-resource triple a labelled edge; literal values
are listed when you tap a node, and tapping an edge shows its predicate and the triple
(scroll to zoom, drag to pan, *Fit*, *Re-arrange*). Nothing about the vocabulary is
interpreted. The file is re-read every 1.5 s; a change is merged into the drawing
without moving the viewport. A missing or empty file is explained; while a file is
malformed (for example being written) or unreadable, the last valid drawing stays,
marked as such. *Mock* shows the GUI's own fixtures (`smpa_gui/mock_graphs/`, reserved
namespace `https://example.invalid/smpa-gui-mock/`) under a **MOCK DATA** banner; they
are never copied into a run, and the GUI never writes any Turtle file.

With `smpa:=false` the Dashboard says that SMPA integration is disabled and nothing is
read or started.

## SMPA tab: Sense workspace (optional)

SMPA integration is optional. The ROS 2, Sensors and Actions tabs never need it, and
the GUI imports no SMPA module. With `smpa:=false` (or `--no-smpa`) the SMPA tab only
says that the integration is disabled and nothing of SMPA is read. Otherwise the tab
reads SMPA's recorded sessions from its memory directory, read-only. When the GUI runs
from the source folder `smpa-ros/smpa_gui`, `smpa-ros/memory` is found automatically;
an installed package has no such neighbour, so give the directory:

```bash
ros2 launch smpa_gui gui.launch.py smpa_memory_root:=$HOME/ros2_ws/src/smpa-ros/memory
SMPA_GUI_MEMORY_ROOT=~/ros2_ws/src/smpa-ros/memory ./run_gui.sh      # from another worktree
```

Without a memory directory the tab says how to configure one.

- **Session selector**: every run under `memory/runs/` (robot and conversation sessions)
  and the `memory/legacy/` archive, newest first, with its activity:
  *Active* (running and its ROS nodes are visible), *Running? not visible* (metadata says
  running but no ROS nodes: possibly stopped without finalizing), *Running (unverified)*
  (conversation sessions: SMPA offers no liveness signal yet), *Ended*, *Unknown*.
- **See**: the session's camera topic live (red **LIVE** frame), and the frames,
  descriptions and focused answers SMPA recorded (**RECORDED BY SMPA**). For an ended
  session the live picture is explicitly "the camera now, not part of the session".
- **Hear**: whether the session's Listen action is executing right now (live, active
  sessions only), and every recording with its player and transcript.
- **Read**: the user messages SMPA's Sense stage observed, with their epistemic status
  ("unverified user statement") and message number; a robot run links to the
  conversation session it read. Messages are entered on the Dashboard.
- **Timeline**: all observations and Sense invocations (including failed ones) in time
  order, filterable by modality; click a row for its source file, Sense record, the
  source of its time stamp and every recorded provenance field.

The GUI writes nothing to the memory directory; only SMPA does (also when SMPA's
conversation server was started from the Dashboard). Demonstration data in SMPA's formats can be
generated and is clearly labelled:

```bash
.venv/bin/python test/fixtures/smpa_memory.py --output /tmp/smpa-demo --demo
SMPA_GUI_MEMORY_ROOT=/tmp/smpa-demo ./run_gui.sh
```

What SMPA would need to provide for exact liveness and simpler integration is described in
[SMPA_CONTRACTS.md](SMPA_CONTRACTS.md).

## Tests

```bash
./test_gui.sh              # unit, ROS integration (real fixture nodes) and packaging tests
./test_gui.sh --browser    # also headless-browser tests (needs Playwright with Chromium)
```

The tests start their own fixture nodes in a random ROS domain restricted to this
computer: talkers, sensors and action servers (`test/fixtures/fixture_nodes.py`), and a
settings node and service providers that the parameter and service tests change and
call (`test/fixtures/management_nodes.py`). The packaging tests build the package with
colcon into a temporary workspace and run it through `ros2 run` and `ros2 launch`; they
are skipped without colcon. Nothing is mocked except where a test says so (the
frontend-logic tests run the page's pure functions in Node.js). The tests do not touch
SMPA or its test runner.

The Dashboard tests run SMPA's real conversation server from an SMPA checkout with its
`.venv` (default: the repository containing `smpa_gui/`; set `SMPA_GUI_TEST_SMPA_ROOT`),
with a test configuration whose only model is a deterministic local stub
(`test/fixtures/openai_stub.py`). They write only to temporary directories and are
skipped when no such checkout is found. One test also checks the GUI's model list
against SMPA's server using SMPA's real configuration; it needs the SMPA workspace
sourced. See [VERIFICATION.md](VERIFICATION.md) for what was and was not verified.

## Known limitations

General

- The GUI never publishes to topics. Its only changes are confirmed action goals,
  parameter changes and service calls.
- There is no user authentication. The server listens on the loopback interface only.
  Binding it elsewhere with `--host` gives that network read access to every topic;
  changes stay restricted to this computer. Any program running on this computer under
  any user can obtain the session token and make changes, exactly as it could with
  `ros2 action send_goal`, `ros2 param set` or `ros2 service call`.
- The graph is refreshed by polling a snapshot every two seconds (a difference stream
  was considered and not needed). Short-lived entities, such as SMPA's per-cycle camera
  subscription and action clients, appear and disappear between refreshes.
- Services are listed in the Services panel but not drawn in the graph.
- ROS discovery does not transmit the history policy or queue depth of other nodes'
  endpoints; they are shown as unknown.
- Parameters can be read only from nodes that are spinning. SMPA's loop and Sense
  nodes do not answer while a model is running inference.
- Like every rclpy node, the gateway node owns a `/parameter_events` publisher, on
  which it never publishes. It has no parameter services and no `/rosout` publisher.
- A topic with several message types is shown using its first type.
- Open sensor panels are remembered while the page is open, not across reloads.

Parameters and services

- Parameter and service calls reach only nodes that are spinning; SMPA's loop and Sense
  nodes do not answer while a model runs inference.
- The GUI's checks cover types, integer/floating-point ranges with steps, read-only and
  dynamic typing. Free-text `additional_constraints` are shown, not checked; the node
  checks them. Nodes without a descriptor answer are checked by the node only.
- A timed-out parameter change or service call has an unknown outcome; read the values
  (or the provider's state) again before deciding to repeat it.
- A provider that is killed (rather than stopped) is noticed only when DDS discovery
  times out, typically after 10–20 s; until then a call waits for its timeout.
- Responses are shown in full; very large responses (for example images) are not
  shortened. Parameter and call histories live in the GUI process only (at most 200
  each) and are lost when it stops.
- Changing the type of a parameter is offered only with dynamic typing; undeclared
  parameters cannot be created.

Actions

- The GUI cannot tell physical from harmless actions and cannot judge whether a goal is
  safe. It checks field types and ranges only, not whether values make sense.
- Cancelling is a request. Whether and how fast a goal stops is up to the action server.
- The goal history lives in the gateway's memory: it is lost when the GUI server stops,
  holds at most 200 goals, and keeps the last 100 feedback messages per goal. Feedback
  and results are shown with long arrays shortened.
- The page shows feedback by polling twice per second; faster feedback is recorded by
  the gateway but displayed in steps.
- Goals sent by other clients (SMPA, `ros2 action send_goal`) are not shown.
- Whole numbers beyond ±2^53 cannot be entered exactly in a browser and are refused by
  the form. NaN and infinity cannot be entered.
- Byte arrays are edited as lists of numbers, which is impractical for large data.
- Action types whose package is not sourced in the GUI's environment are listed but
  cannot be used (see `SMPA_GUI_OVERLAY`).
- An action with several types is used with its first type.
- While a goal is active the gateway appears on the ROS graph as a client of that
  action; the client is removed when its last goal has finished.

SMPA tab

- Only Sense is implemented; Model, Plan and Act are placeholders.
- The adapter follows SMPA's current file formats (see SMPA_CONTRACTS.md §1). If Codex
  changes them, the adapter must follow; damaged or unknown files are shown as such,
  not hidden.
- Liveness of robot runs is inferred from ROS names under `/run_<uuid>`; a run in another
  ROS domain or on another machine looks "not visible". Conversation sessions cannot be
  checked at all (contract C1).
- Older evidence files carry local time without an offset; the run's start offset is
  applied. Focused answers and frozen images have no recorded time and show file
  modification times, labelled as such (contract C3). Copying a memory directory changes
  those times.
- Evidence in older runs is linked to its Sense record only where SMPA recorded the link
  (`source_observations`, `focused_observations[].source_record`).
- A session shows at most its newest 2000 entries; descriptions over 20 000 characters
  are shortened.
- The live camera is the session's configured `camera_topic`; recorded frames are what
  SMPA saved. The GUI does not record anything itself.
- Read is read-only; conversation input is on the Dashboard. Robot runs have no text
  input or session control here (C5).

Dashboard

- SMPA's conversation server answers one request at a time. While it generates a reply,
  the Dashboard shows the last known state and refuses other conversation actions.
- One conversation server per GUI. Two GUIs (or a GUI and SMPA's own conversation page)
  cannot serve the same run: SMPA locks the transcript.
- Starting a conversation starts the server with SMPA's configured default model and
  then switches to the chosen one; both are recorded in the run (C9).
- Liveness of conversation sessions not served by this GUI is still unknown (C1).
- Model descriptions are configuration facts, not curated text (C8).
- Graphs above 5000 triples are truncated (marked). The graph views follow files, not
  cognitive cycles; no history of earlier graph versions is kept or reconstructed.

Sensors

- No coordinate transforms (TF) are applied. Every viewer draws data in the sensor's
  own frame; the frame name is shown.
- Images: `rgb8`, `bgr8`, `rgba8`, `bgra8`, `mono8`, `8UC1` are shown as they are.
  `mono16`, `16UC1` and `32FC1` are contrast-stretched per frame, so brightness is
  relative, not metric. Other encodings (Bayer, YUV, 16-bit colour) are reported as
  unsupported. Frames are sent as JPEG (lossy), at most 1920 px wide and 30 per second.
- Compressed images: JPEG is forwarded unchanged; PNG and other Pillow-readable formats
  are re-encoded. The `compressedDepth` and `theora` transports are not supported.
- Point clouds: needs `x`, `y`, `z` fields. Colour from a packed `rgb`/`rgba` field or
  an `intensity` field, otherwise by height. Clouds are thinned uniformly to at most
  20 000 points (100 000 through the API). Not performance-tested with a real lidar.
- Laser scans: ranges only (no intensities), thinned to 1440 rays.
- IMU: current orientation, turn rate and acceleration; no time plots.
- Joint states: values and a −π…π position scale; no robot model.
- Satellite position: plain coordinate grid by default. The OpenStreetMap background
  is optional, needs internet, sends the viewed area to `tile.openstreetmap.org`, and
  could not be tested in the development environment.
- Camera calibration is shown as numbers; it is not used to undistort the image viewer.
