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
cd <gui worktree>/gui
./install_gui.sh --dev
./test_gui.sh                       # expect 88 passed (83 if Node.js is absent)
./run_gui.sh                        # open http://127.0.0.1:8765, stop with Ctrl+C

# Try the Actions tab against a harmless action server first, for example:
ros2 run action_tutorials_py fibonacci_action_server      # if that package is installed

# With SMPA or any robot running, compare the GUI with:
ros2 node list; ros2 topic list -t; ros2 action list -t
rqt_graph

# Confirm SMPA is unaffected (from the main checkout, unchanged runner):
cd ~/ros2_ws/src/smpa-ros && git status --short && ./test.sh --unit
```
