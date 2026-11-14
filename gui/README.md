# SMPA-ROS GUI

A standalone, browser-based, strictly observational GUI for ROS 2. It runs as its own
process and its own ROS 2 node, imports nothing from SMPA, and works whether or not
SMPA is running.

| Tab | Content |
|---|---|
| **ROS 2** | Graph (rqt_graph-like), Topics (types, endpoints, QoS, live messages), Parameters (read-only), Actions (read-only) |
| **Sensors** | Live viewers for nine `sensor_msgs` types in freely arranged panels |
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
```

Stop it with Ctrl+C. Starting and stopping the GUI does not affect any other ROS
process, and other ROS processes may start and stop while it runs.

| Variable | Meaning | Default |
|---|---|---|
| `SMPA_GUI_HOST`, `SMPA_GUI_PORT` | Listening address | `127.0.0.1`, `8765` |
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
- **Actions**: lists actions with their types, servers and clients.
- **Sensors**: *Open* a discovered topic, or add an empty panel and choose a topic.
  Any number of panels can be open. A panel subscribes only while it is open and the
  Sensors tab is shown. In the 3D viewer: drag rotates, Shift-drag or right-drag pans,
  wheel zooms, double click re-centres.

## Tests

```bash
./test_gui.sh              # unit tests + integration tests against real ROS 2 fixture nodes
./test_gui.sh --browser    # also headless-browser tests (needs Playwright with Chromium)
```

The tests start their own fixture nodes in a random ROS domain restricted to this
computer. They do not touch SMPA or its test runner. See [VERIFICATION.md](VERIFICATION.md)
for what was and was not verified.

## Known limitations

General

- Version 1 is read-only by design: no publishing, service calls, action goals or
  parameter changes.
- There is no authentication. The server listens on the loopback interface only.
  Binding it elsewhere with `--host` gives that network read access to every topic.
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
