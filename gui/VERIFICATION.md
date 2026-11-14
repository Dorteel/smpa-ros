# Verification results (GUI version 1, 2026-11-14)

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

## Automated tests: 49 passed

| Suite | Tests | What it exercises |
|---|---|---|
| `tests/test_units.py` | 10 | Name rules, graph building, bounded message conversion, image/point cloud/scalar sensor encoders, registry |
| `tests/test_frontend_logic.py` | 3 | Graph filters, layered layout including loops, IMU and map conversions (Node.js) |
| `tests/test_ros_integration.py` | 26 | Real server process against real ROS 2 fixture nodes over HTTP and WebSocket |
| `tests/test_browser.py` | 10 | The real frontend in headless Chromium against the same server and fixtures |

The fixture process (`tests/fixtures/fixture_nodes.py`) runs seven real nodes: a talker
and listener, a node with declared parameters, a node that is never spun, an action
server and client (`example_interfaces/action/Fibonacci`), and a sensor node publishing
all nine supported sensor types plus a depth image and an image with an unsupported
encoding.

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
  fresh checkout of `gui/` in the container.

## Not verified

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
./test_gui.sh                       # expect 39 passed (36 if Node.js is absent)
./run_gui.sh                        # open http://127.0.0.1:8765, stop with Ctrl+C

# With SMPA or any robot running, compare the GUI with:
ros2 node list; ros2 topic list -t; ros2 action list -t
rqt_graph

# Confirm SMPA is unaffected (from the main checkout, unchanged runner):
cd ~/ros2_ws/src/smpa-ros && git status --short && ./test.sh --unit
```
