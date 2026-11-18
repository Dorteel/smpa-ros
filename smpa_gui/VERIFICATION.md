# Verification results (GUI version 7, 2026-11-18)

## Version 7: ROS 2 Workspace (phase 1: layout, discovery, inspection)

### Where it ran

**Cloud container only** (same environment and SMPA copy as version 6 below). Nothing
was run on the workstation.

Command: `SMPA_GUI_TEST_SMPA_ROOT=<copy> ./test_gui.sh --browser` with the copy's
`smpa_llm` workspace sourced: **170 passed** (backend, ROS integration, packaging) and
**65 passed** (browser), none skipped. The two Workspace suites were also run twice more on
their own without a failure.

| Suite | Tests | Kind |
|---|---|---|
| `test/test_workspace.py` | 5 | integration with real fixture nodes: topic/service/action routes with types and providers; `GET /ros/parameters` keeps parameters with their nodes, explains the silent node, flags the GUI, stays within one timeout; timeout limits; a real node's topic, service, action and parameter appear and disappear; unit: side-by-side reading, order and isolated failure |
| `test/test_browser_workspace.py` | 14 | browser against real fixture nodes (below) |
| all earlier suites | 165 + 51 | regression; the two subview-list assertions now include *Workspace* |

Requested checks and where they are covered (`test_browser_workspace.py` unless noted):

1. Topics with correct message types in the top panel: `test_topics_appear_with_their_message_types`.
2. Services in the left panel: `test_services_appear_in_the_left_panel`.
3. Actions in the right panel: `test_actions_appear_in_the_right_panel`.
4. Parameters with owning nodes and values: `test_parameters_appear_grouped_by_their_nodes`;
   backend `test_parameter_overview_keeps_parameters_with_their_nodes`.
5. Selecting an interface opens its existing inspector: `test_chips_open_the_existing_inspectors`.
6. Parameter changes use the existing confirmation (nothing changes before *Apply now*;
   the new value appears in the Workspace afterwards):
   `test_parameter_change_from_the_workspace_needs_the_existing_confirmation`.
7. Adding and removing ROS 2 entities updates the panels (gone, then removed):
   `test_panels_follow_entities_appearing_and_disappearing`; backend
   `test_interfaces_appear_and_disappear_with_their_node`. Unreachable gateway:
   `test_unreachable_gateway_is_shown_with_the_last_known_interfaces`.
8. The canvas remains empty: `test_canvas_stays_empty` (and `data-elements` is 0 in every
   viewport check).
9. Zoom (buttons, wheel, keys), pan (drag, arrow keys), reset, fit, grid:
   `test_zoom_pan_reset_and_fit`; viewport kept across refreshes and inspector visits:
   `test_viewport_survives_refreshes_and_inspector_visits`. Layout:
   `test_workspace_has_five_regions_with_the_canvas_dominant`,
   `test_panels_collapse_and_resize`, `test_layout_adapts_to_a_narrow_window`.
10. Existing ROS 2, Sensors, Actions and SMPA functionality: all earlier suites pass unchanged
    except for the subview list.

Not verified: the workstation's ROS installation, a large real system (hundreds of nodes)
and the load the parameter overview puts on it, other browsers than Chromium, touch input.

---

## Version 6: conversational Dashboard integrated into `smpa_gui`

### Where it ran

**Cloud container only** (same environment as version 5 below: RoboStack Jazzy, Python
3.12, headless Chromium, colcon-core 0.21.3). Nothing was run on the workstation.

SMPA used by the tests: a copy of the workstation's `main` 717094b sources needed by the
conversation server (`smpa_ros/smpa_ros/*`, `smpa_ros/memory.py`, `smpa_ros/config.yaml`,
`smpa_llm/` with its `config.yaml` and model registry), laid out like the repository, with
an SMPA-style `.venv` (rdflib 7.1.4, python-dotenv 1.2.4, jsonschema 4.26.0, openai 3.39.0)
and `smpa_llm` built with colcon and sourced, as SMPA's `install.sh` would. No model
weights and no API keys: conversations used the deterministic OpenAI-compatible stub
`test/fixtures/openai_stub.py` through SMPA's real API backend. The conversation tests
also passed against a copy of the earlier flat SMPA layout.

Command: `SMPA_GUI_TEST_SMPA_ROOT=<copy> ./test_gui.sh --browser` with the copy's
`smpa_llm` workspace sourced: **165 passed** (backend, ROS integration, packaging) and
**51 passed** (browser), none skipped.

| Suite | Tests | Kind |
|---|---|---|
| `test/test_dashboard_units.py` | 23 | unit: layout (both SMPA layouts, candidates and fallbacks, refused paths), Turtle reader, transcript reader, catalogue (configuration overlay, registry in smpa_llm, `.env`, real SMPA configuration), server prerequisites |
| `test/test_conversation.py` | 13 | integration with SMPA's real conversation server and the stub |
| `test/test_packaging.py` | 7 | colcon build, `ros2 pkg/run/launch`, include, installed assets (frontend, mock graphs, layouts), and the installed GUI holding a real SMPA conversation |
| `test/test_browser_dashboard.py` | 7 | browser |
| all earlier suites | 118 + 44 | regression |

### Requested checks

1. **Package installation and launch.** colcon build; `ros2 pkg executables`; `ros2 run`;
   `ros2 launch smpa_gui gui.launch.py` with `smpa_layout:=`, from the install space, held
   a conversation with SMPA's server (message saved in the run) and stopped cleanly.
2. **Model discovery and selection.** With SMPA's own `smpa_ros/config.yaml`
   (`inference_config: package://smpa_llm/config.yaml`) the GUI's list equals the agents
   SMPA's server reports (6 local Qwen/Phi/Gemma entries, `api:openai`, `api:nebula`;
   default `local:qwen3-4b`) and is computed without starting anything. Selecting a model
   switches SMPA's agent; a model without weights is refused with SMPA's reason.
3. **Conversation start and message exchange** through SMPA's server; one run per
   start even when the request is repeated; generation progress (busy, elapsed time).
4. **Automatic persistence.** Each exchange is in `<run>/conversation.md`, written by
   SMPA; inference failure keeps the user's message and shows SMPA's explanation.
5. **Reload and resume.** Page reload shows the active conversation; an earlier one is
   shown from its transcript (read via the `conversation.md` fallback), reopened, and
   continued in the same file.
6. **Turtle parsing and visualization**: nodes, labelled edges, literal values, blank
   nodes, truncation; node and edge inspection in the browser.
7. **Switching Sense, Belief, Plan**: three files, three graphs, paths shown.
8. **External updates** appear within about 1.5 s with zoom and pan unchanged.
9. **Missing, empty, malformed, unavailable** files, each explained; the last valid
   drawing is kept for malformed and unreadable files. Belief shows SMPA's
   `belief-state.ttl` (marked as fallback) until `world_model/belief.ttl` exists.
10. **Operation without SMPA**: `--no-smpa` disables every Dashboard route (404) and the
    page shows a note; ROS routes keep working; nothing of SMPA (`smpa_ros`, `smpa_llm`,
    `memory`, …) is imported anywhere in the GUI.
11. **Preservation**: all ROS 2, Sensors, Actions, Parameters, Services and SMPA Sense
    tests pass. Mock graphs never change a run directory (fingerprint test).

### Test changes

- Contract version 1.5; the write-route list includes the five conversation routes.
- Browser tests no longer return Cytoscape objects from `page.evaluate`. Doing so made
  Chromium's renderer crash reproducibly when an edge was tapped, and is the most
  likely cause of the occasional "Target crashed" seen in versions 4 and 5 (two such
  calls existed in `test_browser.py`). No crash occurred after the change.

### Not verified (version 6)

- Anything on the workstation, including SMPA's real `.venv`, model weights, API keys,
  and the installed SMPA workspace there.
- A real model answering (local or API); belief modelling beyond the stub's one triple.
- SMPA producing `world_model/*.ttl` (it does not yet); only the GUI side was tested.
- The complete SMPA tree: the copy held only what the conversation server imports
  (no `sense/`, `speech/`, `rlhf_gym`); robot runs were not started.

# Verification results (GUI version 5, 2026-11-17)

## Version 5: ROS 2 package `smpa_gui`, parameter and service management

Ran in the same cloud container as version 3 (table below), with colcon-core 0.21.3
(installed there for this purpose), setuptools 79.0.1, `launch`/`launch_ros` from
RoboStack Jazzy. **Nothing was run on the workstation**: this session can read and
write files there but cannot execute commands.

### Automated tests: 172 passed

`./test_gui.sh --browser` (final run): 128 passed in the backend command, 44 passed in
the browser command.

| Suite | Tests | Kind | What it exercises |
|---|---|---|---|
| `test/test_units.py` | 16 | unit | earlier units, plus parameter change rules, parameter message construction for every type, service kinds, ROS-argument stripping |
| `test/test_frontend_logic.py` | 6 | unit (Node.js) | earlier logic, plus the page's parameter parsing and checking rules |
| `test/test_ros_integration.py` | 26 | ROS integration | version 1 behaviour; the write-route list now names the parameter and service routes |
| `test/test_actions.py` | 25 | ROS integration | action execution, unchanged |
| `test/test_smpa_runs.py`, `test/test_smpa_integration.py` | 9 + 13 | file / ROS integration | SMPA adapter; new: SMPA integration disabled; no SMPA module imported anywhere |
| `test/test_management.py` | 27 | ROS integration | parameters and services against `test/fixtures/management_nodes.py` (real nodes) |
| `test/test_packaging.py` | 6 | packaging | colcon build, `ros2 pkg`, console script, installed assets, `ros2 run`, `ros2 launch`, inclusion in another launch file, clean shutdown |
| `test/test_browser.py`, `test_browser_actions.py`, `test_browser_smpa.py` | 10 + 12 + 10 | browser | earlier frontend; ROS 2 view list now has Services; SMPA-disabled banner |
| `test/test_browser_management.py` | 12 | browser | Parameters and Services panels against the real management fixtures |

Mocks: none in the integration, packaging and browser tests (real ROS 2 nodes, real
GUI server processes, real colcon builds, real Chromium). The unit and Node.js tests
exercise pure functions.

### Requested checks

Packaging:

- `colcon build --paths smpa_gui` with the GUI's interpreter succeeds; the console
  script's interpreter line is that interpreter.
- `ros2 pkg prefix`, `ros2 pkg list` and `ros2 pkg executables smpa_gui` find the package
  and its `smpa_gui` executable; `share/smpa_gui` holds `package.xml` and
  `launch/gui.launch.py`.
- Every file of the source frontend is installed (same relative paths), the installed
  server serves `index.html` and JavaScript modules from the install space, and
  `smpa_gui.app.FRONTEND` points into the install space when run outside the sources.
- `ros2 run smpa_gui smpa_gui --port P --no-smpa` and
  `ros2 launch smpa_gui gui.launch.py port:=P read_only:=true smpa:=false` start the GUI
  (node `/smpa_gui`), apply the arguments (changes disabled, SMPA disabled), and on
  Ctrl+C to the process group end with status 0, the message "goals settled, node left
  the ROS graph", "process has finished cleanly", and no traceback.
- A separate launch file that includes `gui.launch.py` through `FindPackageShare`
  (the README example) starts the GUI with its arguments passed through.

Parameters (real node `/fixture_mgmt/settings`):

- Discovery of nodes and parameters; values, types, descriptions, read-only, dynamic
  typing, free-text constraints, integer and floating-point ranges with steps.
- Successful atomic change of two parameters, read back by the GUI and by
  `ros2 param get`; a repeated request id changes nothing and is recorded once.
- Arrays (double, string, empty integer array) and a type change with dynamic typing.
- Refused before sending, with nothing changed (values compared before and after):
  read-only, off-step, out of range (integer and double), type change without dynamic
  typing, boolean as number, fraction as integer, wrong array element, unknown parameter.
- The node's own rejection (`forbidden is not allowed here`): atomic → nothing applied;
  individual → partial, with per-parameter answers.
- Missing node and unresponsive node (never spun) → refused, nothing sent; duplicate
  parameter names → 422; missing token → 403; read-only GUI → 403, values unchanged.
- In the browser: constraints shown, no edit for read-only, live problems block Apply,
  declining the dialog changes nothing, a double click on *Apply now* applies once,
  partial results and the node's reason shown, arrays and type choice edited, an
  unresponsive node explained, read-only GUI offers no edits.

Services (real nodes `/fixture_mgmt/services`, `/fixture_vanish/node`):

- Discovery with types, kinds, providers and clients, consistent with
  `ros2 service list`; plumbing hidden by default in the page and shown by check boxes.
- Request/response schemas for `rcl_interfaces/srv/SetParameters` (array of nested
  messages containing arrays); generated form filled in the browser and the complete
  request shown in the dialog.
- Successful calls; nested request arriving intact; exactly one call per confirmation
  (counted by the provider), also for a repeated request id, a double click and a reload.
- Validation errors (text for an integer, out of range, unknown field) → 422 and no call
  (provider count unchanged); declining the dialog → no call.
- Timeout: a 3-second service with a 1-second timeout answers `timed_out` after about
  one second, is called once, and is not retried; with 6 s it succeeds.
- No provider → 409 within two seconds, nothing sent, other requests served meanwhile.
- Provider leaving during a call (stopped with Ctrl+C) → `server_lost` in a few
  seconds instead of the 30-second timeout.
- After calls, the GUI holds no service clients. Read-only GUI → 403 and the page's
  call button disabled.

Regression: all earlier suites pass. Intentional test changes: contract version 1.4,
the write-route list (four routes), the ROS 2 view list (Services added), the SMPA
banner test (disabled case added), paths `tests/` → `test/`, `backend` → `smpa_gui`.

### Test-infrastructure changes and flakiness

- `test_browser.py::test_leaving_the_sensors_tab…` checked the number of restored
  sensor panels immediately after the first one appeared; it failed twice in about ten
  runs (4 of 8 panels counted). It now waits until all panels are back. The Sensors
  code is unchanged since version 1.
- Headless Chromium crashed ("Target crashed", a renderer CHECK failure recorded in the
  kernel log) during `test_browser.py` in two combined runs, as it had once during
  version 4. With `--disable-dev-shm-usage` added to the test browser's launch options,
  five consecutive runs and the final full run passed. This is an environment issue of
  the container, not a GUI behaviour, but it was not proven so.
- The test session now stops the `ros2` daemon of its random ROS domain at the end;
  earlier runs left one daemon per domain. Daemons of the domains of `lonely_server`
  tests can still remain.
- `ros2 param get` and `ros2 service list` comparisons are retried briefly, because
  discovery in a fresh CLI process is asynchronous.
- A first full run exposed that a freshly created parameter-service client may not yet
  match its server; the gateway now waits up to one second (or the timeout) for it
  before reporting `unavailable` (previously it gave up at once).

### Not verified (version 5)

- Anything on the workstation: apt-packaged Jazzy, `/usr/bin/python3`, Ubuntu's
  colcon, and building inside `~/ros2_ws` beside SMPA.
- The rosdep keys `python3-fastapi`, `python3-uvicorn`, `python3-websockets` and the
  Ubuntu versions they install (older than the pinned versions; Pydantic 2 required).
- `colcon test` for `smpa_gui` (the tests are run with `./test_gui.sh`).
- Parameter and service management against SMPA's own nodes, Webots or a robot.
- Killed (not stopped) providers: noticed only after DDS discovery times out.
- The GUI version 4 conversation dashboard on top of version 5 (see MIGRATION.md; a
  trial `git am -3` showed the conflicts listed there).

# Verification results (GUI version 3, 2026-11-15)

## Where the verification ran

All checks below ran in a cloud development container, **not on the target robot
workstation**:

| Item | Value |
|---|---|
| OS | Ubuntu 24.04 |
| ROS 2 | Jazzy from the RoboStack conda channel (`ros-jazzy-ros-base` 0.11.0), `rmw_fastrtps_cpp` |
| Python | 3.12.16, NumPy 2.5.4, Pillow 13.0.0 |
| GUI packages | fastapi 0.163.0, uvicorn 0.54.0, websockets 17.4 |
| Browser | headless Chromium through Playwright (WebGL by software rendering) |
| Node.js | 22 (only for the frontend logic tests) |

The official ROS 2 apt repository was not reachable from that container, so the
Debian-packaged Jazzy that the workstation uses was not available.

## Automated tests: 120 passed

| Suite | Tests | What it exercises |
|---|---|---|
| `tests/test_units.py` | 12 | Name rules, graph building, bounded message conversion, sensor encoders, registry, interface schemas and value validation |
| `tests/test_frontend_logic.py` | 5 | Graph filters, layered layout, IMU and map conversions, goal form logic, saved configurations (Node.js) |
| `tests/test_ros_integration.py` | 26 | Version 1 behaviour: real server process against real ROS 2 fixture nodes over HTTP and WebSocket |
| `tests/test_actions.py` | 25 | Action execution over HTTP against real fixture action servers |
| `tests/test_browser.py` | 10 | Version 1 frontend in headless Chromium |
| `tests/test_browser_actions.py` | 12 | Actions tab in headless Chromium |
| `tests/test_smpa_runs.py` | 9 | SMPA adapter against fixture run directories in SMPA's formats |
| `tests/test_smpa_integration.py` | 11 | SMPA routes of a real server, with a real ROS stand-in for an active session |
| `tests/test_browser_smpa.py` | 10 | SMPA tab (Sense workspace) in headless Chromium |

All earlier tests still pass. Version 3 changed three of them on purpose: the SMPA-tab
check in `test_browser.py` now expects the session selector instead of the empty
placeholder; the contract version checks expect 1.2; and the Actions test for
`--keep-goals-on-exit` now retries its `ros2 topic echo --once` check, which was
occasionally too early after the GUI exited. Version 2 changed two: the tab list now
includes Actions, and the "only GET routes" test now asserts that the two goal routes
are the only non-GET routes.

The fixture process (`tests/fixtures/fixture_nodes.py`) runs seven real nodes: a talker
and listener, a node with declared parameters, a node that is never spun, an action
server node and an action client, and a sensor node publishing all nine supported
sensor types plus a depth image and an image with an unsupported encoding.

The action server node offers two real action servers:

- `/fixture/fibonacci` (`example_interfaces/action/Fibonacci`): one feedback message
  every 0.2 s, cancellable, concurrent goals; a negative order is rejected, order 13
  aborts, order 7 refuses cancel requests.
- `/fixture/lookup_transform` (`tf2_msgs/action/LookupTransform`): a goal with strings,
  a boolean and nested time messages; answers at once and echoes goal values in its result.

### Comparison with the standard command-line tools

These tests assert equality between the GUI's API and `ros2` in the same ROS domain:

| GUI | Compared with | Result |
|---|---|---|
| Node list (non-hidden) | `ros2 node list` | equal (8 nodes) |
| Topic names and types | `ros2 topic list -t --include-hidden-topics` | equal (17 topics) |
| Action names and types | `ros2 action list -t` | equal |
| Action servers and clients | `ros2 action info /fixture/fibonacci` | equal |
| Parameter names of a node | `ros2 param list <node>` | equal |
| A parameter value | `ros2 param get <node> fixture_integer` | equal (42) |
| Gateway subscription present / gone | `ros2 topic info /fixture/scan` subscription count 1 / 0 | as expected |
| GUI node gone after Ctrl+C, others alive | `ros2 node list` | as expected |

Service names were compared by hand once and differed by one entry (53 against 54),
most likely the command-line tool's own node; services are not shown in the GUI and
this was not investigated further.

### Behaviour verified by tests

- **Detachability**: the GUI starts in an empty ROS domain; discovers nodes that start
  later; reports `no_publishers` when they stop; keeps running. Interrupting the GUI
  exits with status 0 and leaves other nodes running.
- **Observational footprint**: while idle the gateway node has no subscriptions and no
  services, and owns only the `/parameter_events` publisher every rclpy node has. The
  HTTP application exposes only `GET` routes. Pages from foreign origins cannot open streams.
- **Graph**: fixture connections are shown; the GUI's own node, `/rosout` and
  `/parameter_events` are hidden by default; search dims non-matching elements; filters
  work; zoom, pan and element positions are unchanged after two automatic refreshes.
- **Topics**: endpoints with QoS; live echo; a latched message is received; large
  messages are shortened.
- **Parameters**: types, values and descriptions; a never-spun node is reported as
  `not_responding` within the timeout; nodes without services as `unavailable`.
- **Sensors**: discovery of all fixture sensor topics; all nine types render in nine
  simultaneous panels; image colours are correct; depth images are marked as
  contrast-stretched; an unsupported encoding is explained without breaking the stream;
  point clouds drop invalid points and honour the point limit; rate limiting holds;
  subscriptions exist only while a panel is open and the Sensors tab is shown; switching
  a panel's topic moves the subscription and clears the old picture; closing panels
  unsubscribes; the tab works with no sensor publishers; the 3D viewer rotates, zooms and pans.
- **Launcher**: `install_gui.sh --dev`, `run_gui.sh` and `test_gui.sh` were run from a
  fresh checkout of `gui/` in the container (fresh install for version 1; `test_gui.sh`
  again for version 2).

### Action execution verified by tests

| Requirement | Evidence |
|---|---|
| Discovery and dynamic interfaces | Both servers listed with type and availability; Goal, Feedback and Result schemas and defaults derived from the installed definitions, including nested messages; schemas of fixed arrays, sequences and arrays of messages checked on `sensor_msgs` types |
| Goal submission | Accepted goal gets a UUID, runs, and ends `succeeded` with result, four feedback messages and ordered timestamps; nested values arrive at the server intact |
| Field validation | Nine wrong-value cases (text for a number, fraction or boolean for an integer, out of range, unknown field, wrong nested type) give `422` naming the field, and no goal is created; the page reports the same before opening the dialog |
| Feedback | Live feedback appears in the page while the goal runs |
| Rejected and aborted goals | Reported as `rejected` and `aborted` |
| Cancellation | Active goal cancelled (`canceled`, answer `accepted`); a server refusing cancellation is reported and the goal completes; finished and unknown goals cannot be cancelled |
| Missing server, unknown type | `409` within the time limit; `404` |
| Concurrent goals | Three goals execute at the same time with separate feedback and results; in the page two run together and cancelling one leaves the other running |
| No accidental resend | Repeated request identifier returns the first goal; reading routes never create goals; three page reloads, back/forward navigation, loading a saved configuration and re-using history values create no goal; a failed `POST` is attempted exactly once and reported as "outcome unknown"; an outage of all API requests followed by recovery creates no goal; declining the dialog or pressing Escape sends nothing; a double click on the confirmation sends one goal |
| Access control | No token, wrong token, foreign `Origin`, foreign `Host`, non-JSON body and malformed request identifiers are refused; a token from an earlier gateway process is refused; `--read-only` refuses goals and disables the button |
| Footprint | The gateway appears as an action client only while a goal is active |
| GUI shutdown | Ctrl+C cancels the active goal (the server's own status topic, read with `ros2 topic echo`, reports status 5); with `--keep-goals-on-exit` the server still reports status 2 (executing) after the GUI exited; a restarted GUI has an empty history |
| Server loss | Killing the action server during a goal marks it `lost` |
| Saved configurations | Save, reset, load and persistence across reloads, without sending |

### SMPA tab (Sense workspace) verified by tests

The fixture memory directory (`tests/fixtures/smpa_memory.py`) holds nine sessions written
in SMPA's formats, each modelled on a real run: an all-senses robot run (vision PNG with
`smpa.camera` metadata, description, audio recording and transcript, two Read messages, a
failed second Sense with its frame retained), the conversation session it read, an October
run with the older `vision/` layout and local-time names, a scene-refinement run (frozen
image, focused answer), a running session with a ROS stand-in (`active_run_node.py`), a
running session without ROS presence, an empty session, damaged metadata, and the
`memory/legacy` archive.

| Requirement | Evidence |
|---|---|
| Four sections, only Sense functional | Model, Plan, Act show placeholders |
| Session selector, existing and active sessions | All nine sessions listed newest first; the active stand-in is recognised from its run-scoped ROS names; a running session without them is "not visible"; a conversation session is "running (unverified)"; without a ROS graph a running robot run is "unverified" |
| See | Recorded frames (decoded PNG) and descriptions, frame without description, focused answer, frozen image; live camera in a separate LIVE frame, labelled "not part of the session" when the session has ended |
| Hear | Recordings served as WAV (byte ranges work) with transcript, model, language and duration; live Listen state follows the stand-in's status topic between listening and idle |
| Read | User messages with epistemic status and message number; robot run links to the conversation it read; no input elements |
| Timeline | Seven entries of the robot run in correct order (newest-first and oldest-first), modality filters, provenance details with Sense record, time-stamp source and transcript fields |
| Recorded versus live | Every recorded item is labelled RECORDED and comes from files; live areas use the existing streams; leaving the session or tab closes them (checked with the gateway's subscription list) |
| Empty, inactive, disconnected | Empty session explains "nothing recorded"; ended and stale sessions explained; damaged metadata shown; unconfigured memory directory explained; lost connection shows a banner and keeps the data |
| Read-only | Fingerprint (content, size, modification time) of the whole memory directory unchanged after reading every session and file, directly and through the server; SMPA routes are GET only |
| File confinement | Traversal, absolute paths, non-media files, missing files and links leading outside the session are refused |
| Demonstration labelling | A directory generated with `--demo` shows the DEMONSTRATION DATA banner |
| Reload | The address restores section and session |

**Check against real SMPA data.** I copied part of the workstation's `memory/` (five robot
runs of different generations and the `memory/legacy` archive) into the test container
and ran the adapter on it. It recognised every layout, read all descriptions, both audio
recordings with transcripts, the refinement run's frozen image and focused answer (linked
to its Sense record), applied the run offsets to local-time names, and the output was
valid JSON. That copy is not part of the repository. Its file modification times are
those of the copy, not the originals.

## Not verified

- **The SMPA tab was not run against a live SMPA session**: no running `run_smpa.sh`, no
  real Listen action, no running conversation GUI. Liveness and live Hear were tested with
  a stand-in that publishes the same ROS names and status topic. The real data check
  above used a copy of finished runs only.
- No real conversation session existed on the workstation, so Read was checked with
  fixture data in the documented format only.
- **No action from the target system was exercised**: not `/look_left` or any other
  `simulation_actions` action, no SMPA speech action, no real robot. The interface
  discovery is generic, but those types were not installed in the test environment.
- Action goals with arrays were exercised in the form component and in message
  construction, but no fixture action server with an array goal was available.
- Stopping the GUI server with SIGTERM or by killing it (only Ctrl+C was tested).
- Bounded strings and bounded sequences in real interfaces (rules tested on schemas only).

- **Nothing was run on the target workstation.** The session had file access to the
  repository but could not execute commands there. Installation and launch with the
  apt-packaged ROS 2 Jazzy, the system Python and its Pillow/NumPy versions are untested.
- **The existing SMPA unit tests (`./test.sh --unit`) were not run**, for the same
  reason. The GUI branch adds files only under `gui/` and changes no existing file.
- **No test against SMPA, Webots, the TIAGo camera topic or any real sensor.** Custom
  message types (`smpa_ros_interfaces`, `simulation_actions`) were not present.
- Other DDS implementations (for example Cyclone DDS), multi-machine discovery, and
  non-default `ROS_DOMAIN_ID` setups beyond the isolated test domains.
- Performance with large images or dense point clouds, and long-running stability.
- Browsers other than headless Chromium; hardware-accelerated WebGL.
- The optional OpenStreetMap background of the satellite position viewer (no internet
  access to the tile server from the test browser).
- Dark colour scheme rendering (only checked in the light scheme).

## Commands to verify on the workstation

```bash
cd <gui worktree>/smpa_gui
./install_gui.sh --dev --build ~/ros2_ws    # venv + colcon build of smpa_gui into ~/ros2_ws/install
source ~/ros2_ws/install/setup.bash          # SMPA workspace (smpa_llm) for the real-configuration test
SMPA_GUI_TEST_SMPA_ROOT=~/ros2_ws/src/smpa-ros ./test_gui.sh                 # expect 165 passed
SMPA_GUI_TEST_SMPA_ROOT=~/ros2_ws/src/smpa-ros SMPA_GUI_BROWSER_PYTHON=<python with playwright> ./test_gui.sh --browser   # expect 51 more
source ~/ros2_ws/install/setup.bash
ros2 pkg executables smpa_gui       # smpa_gui smpa_gui
ros2 launch smpa_gui gui.launch.py smpa_root:=$HOME/ros2_ws/src/smpa-ros
                                    # open http://127.0.0.1:8765 → SMPA → Dashboard, stop with Ctrl+C

# Try parameters and services on harmless nodes first, for example:
ros2 run demo_nodes_cpp parameter_blackboard      # ROS 2 → Parameters → /parameter_blackboard
ros2 run demo_nodes_py add_two_ints_server        # ROS 2 → Services → /add_two_ints

# Try the Actions tab against a harmless action server first, for example:
ros2 run action_tutorials_py fibonacci_action_server      # if that package is installed

# With SMPA or any robot running, compare the GUI with:
ros2 node list; ros2 topic list -t; ros2 action list -t
rqt_graph

# Confirm SMPA is unaffected (from the main checkout, unchanged runner):
cd ~/ros2_ws/src/smpa-ros && git status --short && ./test.sh --unit
```
