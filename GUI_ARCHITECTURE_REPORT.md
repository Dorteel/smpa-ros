# GUI Architecture Report: standalone browser GUI for SMPA-ROS

Date: 2026-11-14. Status: audit and proposal only. No GUI code exists yet and no existing project file was modified.

## 0. How to read this report

- **Verified** means I read it in the source files on 2026-11-14. Nothing was executed: no build, no tests, no `ros2` commands, no git commands. The live ROS graph was never observed.
- **Proposed** means a design decision that needs approval before implementation.
- **The repository changed during this audit.** About twenty files were updated in one step between my two inspections today (a conversational web GUI, an RDF belief graph, the Visual Jev service). Two findings in my earlier Sprint 0 report are therefore out of date: the repo now does contain a browser chat GUI and an RDF belief graph (for conversational runs only).
- Not read in full: the test files, `smpa_ros/backends/visual_jev.py`, `scripts/download_models.py`, `rlhf_gym/training.py`, and most of `README.md`. The sibling package `~/ros2_ws/src/simulation_actions` is outside the connected folder; I saw only its name.

## 1. Existing repository architecture relevant to GUI integration (verified)

### 1.1 Constraints from `AGENTS.md`

- It names Codex as the implementer and the user as architect (`AGENTS.md:13-17`).
- New files, classes, dependencies, public-interface changes and execution-model changes need explicit approval (`AGENTS.md:27-35`).
- Every function needs a narrative docstring written before the code (`AGENTS.md:138-171`).
- ROS topic and service names, message fields, launch parameters and serialized formats must be preserved (`AGENTS.md:226-228`).
- `test.sh` may be changed only with authorization (`AGENTS.md:112`).

### 1.2 Existing GUIs

| GUI | Framework | Process and port | ROS? | Where |
|---|---|---|---|---|
| Conversational GUI (chat plus belief graph) | Python stdlib `http.server`, one static HTML page with inline JS and hand-drawn SVG graph, polling `fetch` | `conversation` entry point, `127.0.0.1:8000` | No ROS initialization | `smpa_ros/conversation_web.py:223-408, 410-470`, `smpa_ros/conversation.html`, `setup.py:31` |
| RLHF Training Gym | Streamlit, separate `.venv-rlhf` | `run_rlhf.sh`, `127.0.0.1:8501` | No | `rlhf_gym/app.py`, `requirements-rlhf.txt` |
| Run configurator | Tkinter desktop app | `scripts/run_configurator.py` | No; spawns `run_smpa.sh` and stops it with SIGINT | `scripts/run_configurator.py:514-560` |

There is no browser interface that talks to ROS 2, and no rosbridge, web_video_server, Foxglove or roslibjs reference anywhere in the repo.

The conversational GUI's HTTP routes are `GET /`, `/api/conversation`, `/api/agents`, `/api/graph`, `/api/sds` and `POST /api/message`, `/api/agent`, `/api/settings`, `/api/new`, `/api/clear`, `/api/resume` (`conversation_web.py:276-289`). It rejects cross-origin POSTs (`conversation_web.py:293-297`), so another origin cannot drive it from a browser.

### 1.3 Execution workflows

| Workflow | Command | What starts |
|---|---|---|
| Robot loop | `./run_smpa.sh [max_cycles:=N service:=vision\|speech\|all]` → `ros2 launch smpa_ros llm.launch.py` → `smpa` | One process: node `smpa_loop`, in-process `sense` node, optional speech participants as a child launch (`runner.py:149-192`) |
| Visual Jev evaluator | `ros2 launch smpa_ros llm.launch.py service:=visual_jev` | Node `visual_jev` (`launch/llm.launch.py:25-30`) |
| Segmentation | `ros2 run smpa_ros segment_image` | Node `segment_image` |
| Speech demo | `ros2 launch smpa_ros speech.launch.py` | Listen and Talk servers |
| Conversation GUI | `.venv/bin/python -m smpa_ros.conversation_web --config "$PWD/config.yaml"` | HTTP server only |

The robot loop is `start → (sense → model → plan → act) × cycles → finish` (`smpa_ros/smpa.py:7-18`). It is bounded by `max_cycles`, reloads the model on every run, and exposes no service, topic or action for external control.

### 1.4 Tests and dependencies

- `./test.sh [--unit|--integration|--all|--rlhf]` does an isolated colcon build, then runs pytest on `test/` only (`test.sh:41-52`). The README describes optional Firefox UI checks gated by `SMPA_BROWSER_TEST=1` (`README.md:1837-1840`).
- Sixteen test files in `test/`, one in `tests_rlhf/`.
- Python dependencies: `setup.py:18-19` (outlines, jsonschema, openai, python-dotenv, rdflib), `requirements-inference.txt`, `requirements-visual-jev.txt`, `speech/requirements-speech.txt`. `requirements-inference.txt` pins `numpy<2` because of the ROS Jazzy ABI.
- `.venv` is created with `--system-site-packages` (`install.sh:36`). When I listed it earlier today it contained no web framework (no FastAPI, uvicorn, aiohttp, Flask, websockets or tornado).
- ROS dependencies: `rclpy`, `sensor_msgs`, `action_msgs`, `launch`, `launch_ros`, `smpa_ros_interfaces` (`package.xml`). `scripts/image_publisher.py` additionally needs `cv_bridge` and OpenCV.

## 2. Available ROS 2 interfaces

### 2.1 Static: defined in source (verified)

**Nodes, services, actions**

| Node name | Provides | Uses | Where |
|---|---|---|---|
| `smpa_loop` | — | Subscribes once per cycle to the camera topic; client of `/sense`; short-lived action clients for movement | `runner.py:170`, `sense.py:245-261`, `act.py:72` |
| `sense` | Service `/sense` (`smpa_ros_interfaces/srv/DescribeScene`) | — | `sense.py:102-172` |
| `listener`, `talker` | Actions `/speech/listen` (`Listen`), `/speech/talk` (`Talk`) | — | `speech/actions.py:25-62` |
| `listener_talker` | — | Action clients for both speech actions | `speech/demo.py:17-25` |
| `visual_jev` | Service `/visual_jev/evaluate` (`EvaluateVisualDecision`) | — | `visual_jev/service.py:32-68` |
| `visual_jev_camera` | — | One camera frame; client of `/visual_jev/evaluate` | `visual_jev/camera.py:20-65` |
| `segment_image` | Service `/segment_image` (`SegmentImage`) | — | `segmentation.py:22-64` |
| `segment_image_client` | — | One camera frame; client of `/segment_image` | `scripts/segment_image_client.py:74-89` |
| `image_publisher` | Publishes `sensor_msgs/Image` on `/tiago/camera/color/image_raw` at 1 Hz | — | `scripts/image_publisher.py` |

**Topics.** The repo publishes nothing except the test `image_publisher`. The only topic it subscribes to is the camera topic, `/tiago/camera/color/image_raw` (`config.yaml:1`), type `sensor_msgs/msg/Image`, with sensor-data QoS, raw transport only. Supported encodings are `rgb8`, `bgr8`, `rgba8`, `bgra8`, `mono8` (`sense.py:83-99`). There is no `image_transport`, no compressed topic and no continuous subscriber.

**Movement actions.** Endpoints `/look_left`, `/look_right`, `/look_ahead`, `/turn_left`, `/turn_right` with types `simulation_actions/action/Look`, `LookAhead`, `Turn` (`plan.py:11-18`). The servers and the types live outside this repo.

**Declared parameters**

| Node | Parameters |
|---|---|
| `sense` | `world_model_dir`, `run_id`, `config`, `model_config`, `device`, `dtype`, `max_new_tokens` |
| `listener` / `talker` | `world_model_dir`, `run_id`, `model`, `model_root`, `audio_device`, `max_duration`, `silence_seconds`, `speech_threshold`, `audio_lock` |
| `listener_talker` | `language`, `max_duration`, `action_timeout`, `turns` |
| `visual_jev` | `model`, `model_root`, `source_dir`, `device`, `max_pixels` |
| `segment_image` | `model_path`, `device`, `threshold` |

**Launch arguments** (`launch/llm.launch.py:72-…`): `service`, `model`, `device`, `source_dir`, `max_pixels`, `run_name`, `max_cycles`, `timeout`, `world_model_root`, `config`, `python`, `model_root`, `audio_lock`, `input_device`, `output_device`, `demo`, `participants_only`, `world_model_dir`, `run_id`.

### 2.2 Runtime only: cannot be known from source

- **Which nodes exist.** SMPA nodes exist only while a run is active; outside a run the SMPA part of the graph is empty.
- **Run-scoped names.** During a run, `/sense`, `/speech/listen` and `/speech/talk` are remapped to `/run_<uuid>/…` (`runner.py:153, 164-166`; `launch/llm.launch.py:34-37`). The UUID is new for every run.
- **Who publishes the camera topic**, at what rate, resolution and QoS, and whether any other image topics exist. That depends on Webots or the robot.
- **The movement action servers** and the field definitions of their types.
- **Parameter values.** Defaults are in source, but values come from launch arguments and process-wide `-p` overrides (`runner.py:167-168`).
- **Transient entities.** The loop's camera subscription and action clients are created and destroyed within a cycle, so a graph view will see them appear and vanish.
- **Responsiveness.** `smpa_loop` and `sense` share one single-threaded executor that is blocked during model inference. Their parameter services will not answer during those periods.
- **DDS environment.** `ROS_DOMAIN_ID`, the RMW implementation and discovery-range settings decide what any GUI process can see.

### 2.3 Non-ROS access to SMPA runtime state (verified)

- **Run directories**: `memory/runs/<timestamp>_<name>_<uuid>/` with `metadata.yaml`, `goal.md`, `parameters.yaml`, `state/cycle_NNNNNN/{sense,model,plan,act}.json`, `world_model/iteration_NNN.md`, `observations/` (`memory.py`).
- **Conversational runs** add `conversation.md`, `conversation-sds.md` and `belief-state.ttl` (RDF Turtle), validated against `smpa_ros/belief-structure.owl`. `belief.graph_view()` already returns `{nodes, edges}` JSON (`belief.py:37-50`).
- **Scope of RDF**: the README states that robot world models remain Markdown and conversational RDF is never fed into robot planning (`README.md:1393-1395`).

These files are the only way to see cognition state today; nothing about it is published on ROS.

## 3. Recommended GUI technology stack (proposed)

### 3.1 Options evaluated

Statements about external packages are from my general knowledge and were not checked on this machine; section 9 has the commands to confirm availability.

| Option | Graph | Topics | Parameters | Actions | Images | Assessment |
|---|---|---|---|---|---|---|
| **A. Own rclpy gateway + static web frontend** | rclpy graph API | Dynamic subscription, JSON conversion | `rcl_interfaces` service clients | `rclpy.action` graph functions | JPEG/MJPEG from the gateway | Full control, one process, read-only by construction, our own contract. Needs a small web server library. |
| B. `rosbridge_suite` + `rosapi` + `web_video_server` + roslibjs | Many `rosapi` calls per node | Good | Via `rosapi`; historically slow or flaky on ROS 2 | Limited discovery | Good (`web_video_server`) | Standard, little backend code. Two extra ROS processes, exposes publish and service-call ability to every browser client, and the contract is rosbridge's, not ours. |
| C. Foxglove bridge + Foxglove app | Built in | Built in | Built in | Partial | Built in | Excellent inspector, but not a GUI we own; an SMPA tab would have to be a Foxglove extension. |
| D. Extend Codex's stdlib server | — | — | — | — | — | Zero dependencies and consistent with the conversation GUI, but it lives inside SMPA core and has no WebSocket support. Violates detachability. |

### 3.2 Recommendation

- **Backend: option A.** One Python process containing one rclpy node (`smpa_gui`) and an HTTP/WebSocket server. Recommended server library: FastAPI with uvicorn.
- **Frontend:** static HTML, CSS and vanilla ES modules; no Node toolchain and no build step.
- **Graph rendering:** Cytoscape.js, vendored as one file, for zoom, pan, layout and filtering.
- **Images:** MJPEG (`multipart/x-mixed-replace`) into a plain `<img>` element, plus a single-frame JPEG endpoint. JPEG encoding with Pillow, which is already a system dependency; no OpenCV or `cv_bridge`.
- **Fallback if no new Python packages are acceptable:** stdlib `ThreadingHTTPServer` with polling and server-sent events instead of WebSockets. Feasible, but more hand-written code.

Option B remains a reasonable alternative if you prefer standard ROS web tooling over a custom contract; the frontend structure in section 4 would be the same.

## 4. Proposed GUI component structure (proposed)

```
gui/
  README.md            how to install, run, stop
  API.md               the HTTP/WebSocket contract, versioned
  requirements.txt     pinned GUI-only Python packages
  run_gui.sh           launcher (sources ROS, starts the gateway)
  test_gui.sh          GUI-only test runner
  .gitignore           ignores gui/.venv
  COLCON_IGNORE        keeps colcon from scanning this directory
  backend/
    app.py             HTTP and WebSocket routes, static file serving
    ros_graph.py       nodes, topics, services, connections
    ros_topics.py      topic details and message echo
    ros_parameters.py  parameter listing and values
    ros_actions.py     action servers, clients, types
    ros_images.py      image subscription and JPEG stream
  frontend/
    index.html
    app.js             tab shell and module registry
    tabs/ros/graph.js, topics.js, parameters.js, actions.js, image.js
    tabs/smpa/index.js placeholder only
    vendor/cytoscape.min.js
  tests/
```

- **Tab modules.** Each frontend module exports an id, a title, and `mount(element, api)` / `unmount()` functions. The shell lists registered modules as tabs. Future SMPA modules register the same way and add backend routes under `/api/v1/smpa/…`, so nothing in the ROS tab changes when they arrive.
- **ROS 2 tab** has five sub-views: Node graph, Topics, Parameters, Actions, Image viewer.
- **SMPA tab** shows a placeholder stating that no modules are installed.

## 5. ROS 2 communication strategy (proposed)

**Principles**

- The gateway is an ordinary ROS 2 participant in its own process. It imports nothing from `smpa_ros`, `runner` or `memory`.
- Version 1 is read-only: no publishers, no service servers, no action goals, no parameter writes.
- Everything is discovered at runtime; no topic, node or run name is hard-coded.
- Subscriptions exist only while a browser view needs them and are destroyed afterwards.

**Per feature**

| Feature | Mechanism | Notes |
|---|---|---|
| Node graph | Poll the rclpy graph API about once per second: node names, publishers, subscribers, services and clients per node. Push only differences to the browser. | Default filters hide the GUI's own node, `/rosout`, `/parameter_events`, hidden action topics and parameter services, as `rqt_graph` does. Action topics collapse into one action edge. |
| Topics | Topic names and types; per-topic publisher and subscriber info including QoS. Echo by creating a subscription with the message class resolved at runtime and QoS matched to the publishers. | Rate-limited (about 5 Hz to the browser); large arrays truncated; image and point-cloud payloads summarised, not dumped. |
| Parameters | Service clients for each node's `list_parameters`, `get_parameters`, `describe_parameters`. | Short timeouts. A node that does not answer (for example SMPA during inference) is shown as "not responding", not as an error. |
| Actions | `rclpy.action` graph queries for names, types, servers and clients per node. Optionally subscribe to each action's status topic to show goal states. | Discovery only; no goals are sent. |
| Image viewer | List topics of type `sensor_msgs/msg/Image` and `CompressedImage`. Subscribe best-effort with depth 1, keep only the latest frame, encode to JPEG, cap frame rate and width. | JPEG `CompressedImage` is passed through unchanged. Same five raw encodings as SMPA to start with. |

**Contract sketch** (all under `/api/v1`, every payload carries `contract_version`)

| Endpoint | Returns |
|---|---|
| `GET /health` | `{status, ros: {ok, domain_id, rmw}}` |
| `GET /ros/graph` | `{nodes:[{id,name,namespace}], topics:[{name,types}], edges:[{node,topic,direction}], services:[…], actions:[…]}` |
| `WS /ros/graph/stream` | Graph differences |
| `GET /ros/topics`, `GET /ros/topics/info?name=` | List; types, publishers, subscribers, QoS |
| `WS /ros/topics/echo?name=` | `{stamp, message, truncated}` |
| `GET /ros/nodes/parameters?node=` | `{status, parameters:[{name,type,value,description}]}` |
| `GET /ros/actions` | `[{name, types, servers, clients}]` |
| `GET /ros/images` | Image topic list |
| `GET /ros/images/stream.mjpg?topic=`, `GET /ros/images/frame.jpg?topic=` | JPEG |

Topic and node names travel as query parameters because they contain slashes.

**Lifecycle**

- GUI starts without SMPA: it shows whatever ROS graph exists, or an empty graph.
- SMPA starts or stops while the GUI runs: nodes appear and disappear through normal discovery.
- GUI stops while SMPA runs: its subscriptions vanish; SMPA is unaffected because it never depends on the GUI.

## 6. Dependency and isolation considerations (proposed)

- **Own environment.** `gui/.venv`, created with `--system-site-packages` so `rclpy` and Pillow resolve from ROS and the system. Nothing is installed into Codex's `.venv` or `.venv-rlhf`.
- **No NumPy, Torch or OpenCV** in the GUI requirements, to stay clear of the `numpy<2` ABI constraint.
- **New Python packages** (need approval): `fastapi`, `uvicorn`, and a WebSocket library, pinned in `gui/requirements.txt`.
- **Vendored frontend library**: one Cytoscape.js file, committed, no CDN at runtime.
- **No edits to shared files**: `setup.py`, `package.xml`, `test.sh`, `install.sh`, `.gitignore`, `README.md`, `AGENTS.md`, `config.yaml` stay untouched. The GUI is not installed by colcon; `gui/COLCON_IGNORE` keeps it out of builds.
- **Ports**: 8000 (conversation) and 8501 (RLHF gym) are taken. Proposed default 8765, configurable, bound to `127.0.0.1` only.
- **Security**: loopback only; origin check on WebSocket connections; no write operations in version 1.
- **ROS environment**: `run_gui.sh` sources `/opt/ros/jazzy/setup.bash` and, if present, the workspace overlay so custom message types can be decoded. Without the overlay, topics with unknown types are listed but cannot be echoed. It inherits `ROS_DOMAIN_ID` and RMW settings from the shell, which must match SMPA's.

## 7. Testing strategy (proposed)

- **Unit tests without ROS**: pure functions that turn discovery data into the graph contract, convert messages to JSON with truncation, and convert raw images to JPEG.
- **ROS integration tests**: start small fixture nodes (publisher, subscriber, parameter node, image publisher, action server) in an isolated `ROS_DOMAIN_ID`, start the gateway, and assert the API output. Fixture message types come from standard packages; which ones are installed must be checked first.
- **Contract tests**: every endpoint response is checked against the schemas in `API.md`.
- **Detachability tests**: gateway starts with no other nodes; fixture nodes start and stop while it runs; gateway is killed while fixtures keep running.
- **Frontend smoke test**: optional headless-browser check that each tab mounts and renders fixture data.
- **Manual acceptance**: compare each view with `rqt_graph`, `ros2 topic list -t`, `ros2 param list`, `ros2 action list -t` and `rqt_image_view`.
- **Runner**: `gui/test_gui.sh`, so the shared `test.sh` is not modified. Existing `./test.sh --unit` must still pass unchanged after every GUI change.

## 8. Implementation roadmap (proposed)

| Step | Content | Done when |
|---|---|---|
| G0 | Approvals from section 10; confirm runtime inventory with the commands in section 9 | Decisions recorded |
| G1 | Skeleton: environment, launcher, gateway with `/health`, tab shell with empty ROS 2 and SMPA tabs | GUI starts and stops with and without ROS nodes present |
| G2 | Node graph: snapshot endpoint, difference stream, Cytoscape view with zoom, pan, filters | Matches `rqt_graph` on a fixture graph |
| G3 | Topics: list, info, echo | Matches `ros2 topic` output on fixtures |
| G4 | Parameters: per-node list and values, non-responding state | Matches `ros2 param` output |
| G5 | Actions: servers, clients, types | Matches `ros2 action list -t` |
| G6 | Image viewer: topic picker and live stream | Shows `image_publisher` output |
| G7 | SMPA tab modules (separate task, after agreeing an interface with Codex) | — |

## 9. Commands to confirm the runtime picture (not run)

```bash
source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
echo "$ROS_DOMAIN_ID $RMW_IMPLEMENTATION"
ros2 node list --no-daemon
ros2 topic list -t --no-daemon
ros2 service list -t --no-daemon
ros2 action list -t --no-daemon
ros2 topic info -v /tiago/camera/color/image_raw --no-daemon
ros2 interface show simulation_actions/action/Look
ros2 param list --no-daemon
apt-cache policy ros-jazzy-rosbridge-suite ros-jazzy-web-video-server ros-jazzy-foxglove-bridge
python3 -c "import rosidl_runtime_py, PIL; print('ok')"
cd ~/ros2_ws/src/smpa-ros && git status --short && git log -5 --oneline && ./test.sh --unit
```

Run the `ros2` commands once with SMPA idle and once during `./run_smpa.sh max_cycles:=1` to see the run-scoped names.

## 10. Potential conflicts with Codex's architecture work

1. **Overlapping GUI.** Codex has already put a browser GUI inside the core package (`conversation_web.py`, `conversation.html`) with chat and a belief-graph view. A future SMPA tab would overlap with it. Decision needed: who owns SMPA visualisation, and whether Codex's server will expose a stable, cross-origin-readable interface or the GUI reads run files directly.
2. **A moving base.** The repository changed substantially while I was auditing it. Interfaces listed here (routes, file formats, node names, launch arguments) can change again without notice. The ROS 2 tab is immune because it discovers everything; SMPA modules are not.
3. **Shared files and environments.** Both agents could want to edit `setup.py`, `test.sh`, `.gitignore`, `README.md` or install into `.venv`. This proposal avoids all of them; if that ever becomes impossible I will ask rather than edit.
4. **`AGENTS.md` scope.** It addresses Codex and limits work to one authorized file. Decision needed: does it bind GUI work inside `gui/`? I propose to follow its narrative-docstring rule and its ban on touching existing files, but to treat `gui/` as a pre-authorized multi-file scope.
5. **Git.** Two agents in one working tree risk mixed commits. A separate branch or worktree for `gui/` is advisable.
6. **Runtime side effects of observing.** The GUI adds one node to the graph, one extra best-effort subscriber on any viewed topic, and occasional parameter-service requests to SMPA nodes. These do not change SMPA's behaviour, but they are visible in Codex's own graph inspection and add camera bandwidth while the image viewer is open.
7. **Ports.** 8000 and 8501 are in use by existing tools; the GUI must not default to either.
8. **Control features later.** Starting or stopping the loop, sending action goals or setting parameters from the GUI would touch the execution model that Codex owns. They are excluded from this proposal.

## 11. Decisions requested before implementation

1. Stack: own rclpy gateway (recommended) or rosbridge with web_video_server.
2. Server library: FastAPI and uvicorn (recommended) or stdlib only.
3. Permission to create `gui/` with its own environment, requirements and test runner, and to vendor Cytoscape.js.
4. Whether `AGENTS.md` rules apply inside `gui/` (point 4 above).
5. Ownership of SMPA visualisation relative to Codex's conversation GUI (point 1 above).
6. Default port (proposed 8765).
