# SMPA-ROS GUI

A standalone, browser-based GUI for ROS 2. It runs as its own process and its own
ROS 2 node, imports nothing from SMPA, and works whether or not SMPA is running. It
observes the ROS graph; its only way of acting on it is sending and cancelling action
goals, and only when a user on this computer explicitly confirms each goal.

| Tab | Content |
|---|---|
| **ROS 2** | Graph (rqt_graph-like), Topics (types, endpoints, QoS, live messages), Parameters (read-only), Actions (read-only) |
| **Sensors** | Live viewers for nine `sensor_msgs` types in freely arranged panels |
| **Actions** | Discover action servers, edit a goal in a generated form or as JSON, send and cancel goals, watch feedback, result and history |
| **SMPA** | Empty workspace reserved for future SMPA modules |

Further documentation: [ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md),
[VERIFICATION.md](VERIFICATION.md).

## Requirements

- Linux with ROS 2 (developed for Jazzy) providing `rclpy`, `rosidl_runtime_py`,
  `sensor_msgs`, NumPy and Pillow. All of these come with a normal ROS 2 desktop or
  base installation plus `python3-pil`.
- Python 3.12 with the `venv` module (`sudo apt install python3-venv` on Ubuntu).
- A current browser with WebGL (needed only for the 3D point cloud viewer).

Nothing outside `gui/` is installed or modified. The GUI's Python packages (FastAPI,
uvicorn, websockets) go into `gui/.venv`; SMPA's `.venv` is not used.

## Install

```bash
cd gui
./install_gui.sh          # creates gui/.venv and installs requirements.txt
./install_gui.sh --dev    # additionally installs what the tests need
```

The script uses the ROS 2 environment that is already sourced, or sources
`/opt/ros/$ROS_DISTRO/setup.bash` (default `jazzy`). Set `SMPA_GUI_ROS_SETUP` to use
another `setup.bash`.

## Run and stop

```bash
./run_gui.sh                       # then open http://127.0.0.1:8765
./run_gui.sh --port 9000           # another port
./run_gui.sh --read-only           # inspection only: sending and cancelling goals is disabled
./run_gui.sh --keep-goals-on-exit  # do not cancel active goals when the GUI stops
```

Stop it with Ctrl+C. Starting and stopping the GUI does not affect any other ROS
process, and other ROS processes may start and stop while it runs.

| Variable | Meaning | Default |
|---|---|---|
| `SMPA_GUI_HOST`, `SMPA_GUI_PORT` | Listening address | `127.0.0.1`, `8765` |
| `SMPA_GUI_READ_ONLY=1` | Same as `--read-only` | off |
| `SMPA_GUI_KEEP_GOALS_ON_EXIT=1` | Same as `--keep-goals-on-exit` | off |
| `SMPA_GUI_ROS_SETUP` | ROS 2 `setup.bash` to source | `/opt/ros/$ROS_DISTRO/setup.bash` |
| `SMPA_GUI_OVERLAY` | Workspace `install/setup.bash` with custom message types, or `none` | the enclosing colcon workspace, if the checkout lies under a `src/` directory |
| `ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, discovery settings | Standard ROS 2 settings; must match the system you want to observe | inherited |

Topics whose message type is not available in the GUI's environment are still listed,
but their messages cannot be displayed. To see SMPA's own interfaces or the Webots
action types, point `SMPA_GUI_OVERLAY` at the workspace that built them, for example
`SMPA_GUI_OVERLAY=~/ros2_ws/install/setup.bash ./run_gui.sh`.

If the GUI is checked out as a Git worktree, keep that worktree outside the colcon
workspace (or outside any directory colcon scans), because it contains a second copy
of the `smpa_ros` package sources. `gui/COLCON_IGNORE` only protects `gui/` itself.

## Using it

- **Graph**: drag to pan, wheel to zoom, drag elements to rearrange. Search dims
  everything that does not match. The check boxes hide or show topics, actions,
  unconnected topics, `/rosout` and `/parameter_events`, hidden names, and the GUI's
  own node. The graph refreshes every two seconds without changing zoom or pan.
- **Topics**: select a topic to see its publishers and subscribers with QoS. *Start
  echo* subscribes and shows live messages; *Stop echo* unsubscribes.
- **Parameters**: select a node to read its parameters once. A node that does not
  answer is reported as such; use *Refresh* to try again.
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
- The gateway can send and cancel action goals. It cannot publish to topics, call
  services or write parameters.

### What happens to active goals

| Event | Effect on goals that are still active |
|---|---|
| Browser tab closed, reloaded, or network to the GUI server lost | Nothing. Goals keep running; the gateway keeps recording feedback and results. Reopen the page to see them. Nothing is resent. |
| GUI server stopped normally (Ctrl+C) | The gateway asks each action server to cancel its active goals and waits up to two seconds for the answers, then exits. A server may refuse or take time to stop. With `--keep-goals-on-exit` nothing is cancelled and the goals continue on their servers. |
| GUI server killed or crashed | No cancel request is sent. Goals continue on their servers until they finish or another client cancels them. |
| GUI server restarted | The history starts empty and earlier goals cannot be monitored or cancelled from the GUI any more (use `ros2 action` tools). Pages still open from before must be reloaded before they can send again. |
| Action server disappears during a goal | The goal is marked `lost`: its outcome is unknown. |

## Tests

```bash
./test_gui.sh              # unit tests + integration tests against real ROS 2 fixture nodes and action servers
./test_gui.sh --browser    # also headless-browser tests (needs Playwright with Chromium)
```

The tests start their own fixture nodes in a random ROS domain restricted to this
computer. They do not touch SMPA or its test runner. See [VERIFICATION.md](VERIFICATION.md)
for what was and was not verified.

## Known limitations

General

- Apart from action goals, the GUI is read-only: no publishing, service calls or
  parameter changes.
- There is no user authentication. The server listens on the loopback interface only.
  Binding it elsewhere with `--host` gives that network read access to every topic;
  action goals stay restricted to this computer. Any program running on this computer
  under any user can obtain the session token and send goals, exactly as it could with
  `ros2 action send_goal`.
- The graph is refreshed by polling a snapshot every two seconds (a difference stream
  was considered and not needed). Short-lived entities, such as SMPA's per-cycle camera
  subscription and action clients, appear and disappear between refreshes.
- Services are reported by the API but not drawn in the graph.
- ROS discovery does not transmit the history policy or queue depth of other nodes'
  endpoints; they are shown as unknown.
- Parameters can be read only from nodes that are spinning. SMPA's loop and Sense
  nodes do not answer while a model is running inference.
- Like every rclpy node, the gateway node owns a `/parameter_events` publisher, on
  which it never publishes. It has no parameter services and no `/rosout` publisher.
- A topic with several message types is shown using its first type.
- Open sensor panels are remembered while the page is open, not across reloads.

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
