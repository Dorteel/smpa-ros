# Minimal robot cognition

## Quick start: Ubuntu 24.04 / ROS 2 Jazzy

Use Ubuntu 24.04, Python 3.12, and [ROS 2 Jazzy](https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html).
After installing ROS, install the system build tools explicitly:

```bash
sudo apt update
sudo apt install git build-essential python3-venv python3-pip \
  python3-colcon-common-extensions libportaudio2 \
  ros-jazzy-rclpy ros-jazzy-sensor-msgs ros-jazzy-action-msgs \
  ros-jazzy-launch-ros ros-jazzy-ament-cmake ros-jazzy-ament-cmake-python \
  ros-jazzy-rosidl-default-generators
mkdir -p ~/ros2_ws/src
cd ~/ros2_ws/src
# The existing Webots checkout is also required for its generated interfaces.
git clone --branch master https://github.com/Dorteel/webots_ros2_simulation.git
git clone https://github.com/Dorteel/smpa-ros.git
cd smpa-ros
./install.sh
./run_smpa.sh
```

The scripts resolve the workspace from `<workspace>/src/<checkout>`; no username
or workspace name is hardcoded. The resulting layout is:

```text
ros2_ws/
├── build/                     # Incremental compilation
├── install/                   # ROS package installation and setup.bash
├── log/                       # Colcon logs
└── src/smpa-ros/
    ├── .venv/                 # Python environment with system ROS access
    ├── models/                # Existing model layout and download report
    └── memory/                # Execution records
```

`install.sh` creates `.venv` with `--system-site-packages`, installs ordinary
Python dependencies using its pip, reuses `requirements-speech.txt` and the
vision requirements documented below, downloads the selected weights, and builds
both SMPA packages plus the upstream simulation action interfaces from the workspace root. Pillow, PyYAML, and pytest are not
required in system Python; pytest is only needed if you choose to run tests.
Already satisfied dependencies and verified weights are reused. The script never
installs system packages or edits shell configuration. Existing dependency ranges
are retained; this is not a fully version-locked environment.

`run_smpa.sh` only sources Jazzy and the workspace overlay, checks the installed
SMPA packages and virtual environment, and launches `llm.launch.py`. It never
builds, installs dependencies, or downloads models. Rerun `./install.sh` after
changing code or installed assets. Old repository-level `build/`, `install/`, and
`log/` directories are left untouched and are not used; start from a fresh terminal
if you previously sourced that old overlay.

### Python dependency compatibility

`setup.py` is the single declaration for `setuptools>=30.3,<80`,
`outlines==1.3.3`, and `jsonschema==4.26.0`. The setuptools upper bound matches
colcon-core 0.21.0 and is enforced even when reinstalling into an existing `.venv`.
The installer generates `.venv/smpa-constraints.txt` from that declaration,
configures only `.venv/pip.conf` to use it for subsequent pip operations, and
installs the declared dependencies. A request to upgrade setuptools beyond the
bound fails resolution instead of silently breaking colcon. Do not override the
constraint through pip environment variables or configuration. Rerun installation
if the checkout moves so that the stored absolute constraint path is refreshed.
System-wide Python packages and pip configuration are not changed.

Outlines 1.3.3 selects outlines_core 0.2.14; the direct integration was verified
with Transformers 4.57.6 and the local Qwen3-0.6B model on CPU. The existing
Transformers range remains `>=4.57,<5`. Other transitive dependencies are not
fully locked.

```bash
./install.sh                    # Also repairs an incompatible setuptools version
.venv/bin/python -m pip check  # Optional global diagnostic
.venv/bin/python -c 'from importlib.metadata import version; print(version("outlines"), version("setuptools"))'
```

Installation verifies SMPA's required imports, structured-generation APIs, and
version constraints from the existing dependency declarations before downloads
or compilation. It also checks the dependencies of those required packages,
including ROS `rclpy` and colcon-core. Missing imports or incompatible versions
in this required dependency graph stop installation with specific errors; a real
SMPA/ROS dependency conflict must be resolved rather than bypassed.

`--system-site-packages` remains enabled so ROS 2 Jazzy is accessible. Unrelated
system packages (such as Jupyter, Flask typing stubs, or matplotlib consumers)
are not part of SMPA's verification and do not block installation. No extra
applications are installed merely to satisfy their dependency reports.
`pip check` remains an optional manual diagnostic covering the entire visible
Python environment; its global exit status is not an installation gate. If you
run it, distinguish required-package errors from unrelated system reports, and
retain the setuptools constraint and system ROS packages.

### Models and backend prerequisites

Installation reads `config/pipeline.yaml` and the existing model YAML files.
The default vision launch always needs the configured vision model. Transformers
text models also need local weights; Ollama text models do not, so their weights
are not downloaded by this installer. Unsupported local model IDs or text paths
that disagree with the downloader layout produce an error.

```bash
./install.sh --all-models       # Explicitly download the entire eleven-model library
./install.sh                    # Reuse verified downloads and incrementally build
.venv/bin/python scripts/download_models.py --model Qwen/Qwen3-0.6B
```

The downloader needs internet access and free space including its 5 GiB reserve.
Sense resolves completed downloads through `models/download_report.json`, then
falls back to the existing Hugging Face cache; runtime loading stays offline.
Rerun installation after moving a checkout to refresh recorded paths. Speech
weights are not part of the eleven-model library: provision Whisper/Piper as
shown in the speech section before using `service:=speech` or `service:=all`.

A successful build alone does **not** mean a full SMPA run is ready:

- **Transformers planning:** installation provides Outlines 1.3.3 and
  jsonschema 4.26.0 directly; no external planning project is needed. Plan uses
  token-level JSON Schema constraints and validates the completed JSON response.
  The existing loaded text model/tokenizer are reused, and CPU execution is
  supported without CUDA. Missing dependencies, unsupported schemas, incomplete
  JSON, or validation failures stop generation without an unrestricted fallback.
- **Ollama:** text generation needs a running server at `http://localhost:11434`
  with the configured model already installed (provision it using Ollama's own
  tools). It does not need local Transformers text weights or CUDA in this
  process. However, the existing loop explicitly rejects Ollama at constrained
  Plan, so it cannot currently complete `run_smpa.sh`. Installation supports
  preparing this backend and warns about that limitation. Enabling Ollama planning
  would require Python implementation changes outside this task's scope.
- **Vision hardware:** `vision.device: auto` selects CUDA when available and
  otherwise CPU. Automatic precision is float16 on CUDA and float32 on CPU.
  CPU execution does not need an NVIDIA driver or CUDA libraries; explicitly
  requesting CUDA requires both a working driver and CUDA-enabled PyTorch.
  The Transformers text backend also supports CPU execution.
- **Simulation:** every full cycle, including speech-only, needs the generated
  `simulation_actions/action/Look` interface and running `/look_left` and
  `/look_right` servers. Vision also needs the configured image topic (default
  `/tiago/camera/color/image_raw`). Build and source the simulation workspace and
  start its servers before launching; these scripts do not start the simulation.

Check the external prerequisites after sourcing ROS and the relevant workspaces:

```bash
.venv/bin/python -c 'from outlines import from_transformers; from outlines.backends import get_json_schema_logits_processor'
.venv/bin/python -c 'import torch; print(torch.cuda.is_available())'
ros2 interface show simulation_actions/action/Look
ros2 action list -t
ros2 topic list -t
```

### Run and troubleshoot

Defaults are `service:=vision max_cycles:=1`, this repository's pipeline
configuration, and `.venv/bin/python`. Optional launch arguments override defaults:

```bash
./run_smpa.sh
./run_smpa.sh max_cycles:=3 run_name:=experiment_01
./run_smpa.sh service:=speech max_cycles:=2
./run_smpa.sh config:=/absolute/path/to/pipeline.yaml
```

Existing arguments such as `model_root`, `world_model_root`, and audio devices
are forwarded unchanged. `model_root` controls text/speech models; vision uses
the download report beside the pipeline's project root or in the checkout.

- **Missing `.venv` or Python dependencies:** run `./install.sh`. An existing
  virtual environment must use Python 3.12 with system-site-packages; repair or
  move aside an incompatible environment before reinstalling.
- **PortAudio library not found:** the declared speech dependencies need the native
  library; install it with `sudo apt install libportaudio2`, then rerun
  `./install.sh`. This is a real SMPA dependency failure, not an unrelated
  system-package conflict.
- **Missing compiled packages or launch files:** run `./install.sh`; execution
  does not rebuild. Source any external workspace needed by planning/simulation.
- **Stale repository overlay:** open a fresh terminal without sourcing
  `src/smpa-ros/install/setup.bash`, then rerun installation. The scripts reject
  detected stale overlays rather than using the wrong package installation.
- **Missing model weights:** rerun installation and inspect
  `models/download_report.json`; check the configured model and available disk
  space. For Ollama, check the running server and its installed models.
- **Planning import errors, camera/action waits, or CUDA failures:** rerun installation for Outlines dependencies or resolve the
  corresponding simulation/CUDA prerequisites above; installation does not
  provision drivers or running services.

For optional automated checks, install pytest separately and run
`./test.sh --unit`. These existing tests use a temporary workspace and mocked
models/devices; they do not verify real inference or simulation movement.


## Temporary Webots Integration

This experiment uses [Dorteel/webots_ros2_simulation](https://github.com/Dorteel/webots_ros2_simulation),
branch **master** (inspected commit `f8a975f1572e248f1ab7614bbc69e9ff6fc619e9`).
Keep it beside SMPA in `<workspace>/src/`. The original simulation sources and
servers are reused unchanged; SMPA does not launch Webots or implement servers.

```text
<workspace>/src/
├── webots_ros2_simulation/simulation_actions/  # The only interface package source
└── smpa-ros/temp/simulation_actions/
    └── README.md                             # Provenance/removal notes, not a ROS package
```

No vendoring is necessary. `install.sh` explicitly builds the upstream
`simulation_actions` package alongside SMPA, generating Python modules and native
ROS type support under the workspace-level `install/`. It reconfigures an old
CMake cache if it points at a different checkout. This fixes the observed missing
`Look` import: an older copy from another project's `external/` directory had
produced the stale installation. No duplicate package is created under `temp/`.
For another source location, set `SIMULATION_ACTIONS_SOURCE` to the actual
upstream `simulation_actions` directory when running `./install.sh`.

Install/build from clean ROS Jazzy terminals (skip cloning an existing checkout):

```bash
cd ~/ros2_ws/src
git clone --branch master https://github.com/Dorteel/webots_ros2_simulation.git
cd webots_ros2_simulation
# First-time system setup; these upstream scripts request confirmation:
./install-webots.sh
./install-dependencies.sh
cd ../smpa-ros
./install.sh
```

The simulation needs Webots R2025a, working OpenGL/display access, and the basic
full-robot profile with cameras/head joints. Build and launch it separately:

```bash
cd ~/ros2_ws/src/webots_ros2_simulation
source /opt/ros/jazzy/setup.bash
export SIM_WORKSPACE="$(cd ../.. && pwd)"
./run_simulation.sh --build-only
./run_simulation.sh
```

Use the same `SIM_WORKSPACE` for both commands to avoid a second repository-level
installation. Keep the simulation stepping and use the same `ROS_DOMAIN_ID` in
both terminals. Do not launch a second apartment if Webots is already running.
The mapping/base-only profile is unsuitable: it lacks the required camera/head.

In the SMPA terminal:

```bash
cd ~/ros2_ws/src/smpa-ros
source /opt/ros/jazzy/setup.bash
source ../../install/setup.bash
ros2 interface show simulation_actions/action/Look
ros2 action list -t
ros2 topic list -t
./run_smpa.sh max_cycles:=1
```

The normal Outlines planner still chooses `/look_left` or `/look_right`; no
predefined-action mode or planning change is needed. `Look.Goal()` sends
`angle: 0.0`, meaning the server's configured default (30 degrees in the inspected
simulation), not zero movement. `/look_ahead` uses the distinct `LookAhead` type;
that interface is built too, but it is not added to the planner's action set.
Both installation and execution check generated action type support with SMPA's
`.venv/bin/python`, so stale imports fail with a repair instruction before inference.

Inspect the run directory printed by the loop:

- `observations/vision/*.md`: actual camera description.
- `world_model/iteration_001.md`: the generated environment snapshot.
- `state/cycle_000001/plan.json`: selected action.
- `state/cycle_000001/act.json`: serialized goal, acceptance, received feedback,
  ROS result status, and the actual `success`, `message`, and `achieved_angle` result.
- `metadata.yaml`: `status: completed` and `completed_cycles: 1` after Act succeeds.

Success requires both ROS status **4 (SUCCEEDED)** and the server result's
`success: true`. A rejected, aborted, timed-out, or false-success result cannot
complete a cycle. Do not infer success from the launch process exit status alone.
Cancellation/error recording remains in place. The world-model
snapshot is generated before Plan/Act; execution evidence is saved separately in
`act.json`, preserving the existing output structure. A successful result with
zero achieved angle can mean the head was already at a mechanical limit.

On CPU, inference can be slow. For a shared simulation machine, optionally set
`export OMP_NUM_THREADS=2 MKL_NUM_THREADS=2` before launching SMPA to limit CPU
contention. The unchanged default Sense timeout is 120 seconds per wait; the
first live CPU run timed out before any action was sent. The existing loop timeout
is now exposed as an optional launch argument for this slow-machine experiment:

```bash
OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 ./run_smpa.sh max_cycles:=1 timeout:=600 run_name:=webots_integration
```

This only increases the camera/Sense wait budget; `execution.action_timeout` still
bounds action waits separately. No model, generation setting, planning algorithm,
or default timeout is changed.

Verified against the running Webots simulation on 2026-10-27 using the command
above: real CPU Sense took 146.4 seconds, Model and constrained Plan completed,
`/look_left` accepted `angle: 0.0`, and the server returned ROS status 4,
`success: true`, and `achieved_angle: 0.5235988575` radians (about 30 degrees).
Feedback reported approximately `8.19e-8` radians remaining. The run stopped with
`completed_cycles: 1` and `status: completed`; no mocked action server was used.
Evidence is in the local, Git-ignored run directory:

```text
memory/runs/2026-10-27_121944_844131+0100_webots_integration_a6b98c22-e653-4166-b08d-9e9a1e8de87e/
```

To inspect any run, replace the path with the directory printed by the loop:

```bash
RUN_DIR="memory/runs/2026-10-27_121944_844131+0100_webots_integration_a6b98c22-e653-4166-b08d-9e9a1e8de87e"
cat "$RUN_DIR/metadata.yaml"
cat "$RUN_DIR/state/cycle_000001/act.json"
cat "$RUN_DIR/world_model/iteration_001.md"
```

Existing regressions passed with `ROS_DOMAIN_ID=86 ./test.sh --unit` (81 tests).
An initial run on the live simulation domain crashed in native ROS speech-node
teardown; the isolated-domain rerun passed without changing any tests.

To remove the temporary integration later, remove `temp/simulation_actions/`
and the upstream-source build/preflight blocks in `install.sh`/`run_smpa.sh`
after providing the replacement action interface. The current fixed Look
registry/executor still requires generated `simulation_actions`; replacing that
robot-control design is a separate task.

## SMPA execution and run storage

`smpa_ros/loop.py` is the single orchestrator. It creates a UUID and run directory,
starts the operation servers with that assignment, executes Sense → Model → Plan
→ Act sequentially, counts complete cycles, stops at the configured limit, cleans
up its servers, and finalizes metadata. Launch files only start and configure
processes. Neither services nor action callbacks create runs or initiate cycles.

```yaml
# config/pipeline.yaml
execution:
  max_cycles: 1
```

The default is one cycle. Each camera cycle calls the existing Sense service;
each speech cycle calls the existing Listen action. `service:=all` collects both.
Model uses a local LLM to create a complete Markdown snapshot each cycle. Plan
reads that snapshot and the run's `goal.md`, then uses the same loaded local text
model with JSON Schema-constrained generation to choose exactly one action:
`look_left` (`/look_left`) or `look_right` (`/look_right`). Act sends the default goal (`angle: 0.0`)
to `simulation_actions/action/Look` and waits for ROS status SUCCEEDED before the
next cycle captures a fresh image. Rejected, aborted, unavailable, or timed-out
actions stop the run; active goals are canceled on early exit when possible.
Cancellation failures are recorded rather than reported as success.

The goal is initialized from `goal` in `pipeline.yaml`; it is never autonomously
modified. Plan reads the file each cycle, so an explicit user edit is observable.
`execution.action_timeout` (default 30 seconds) bounds server, acceptance, result,
and cancellation waits separately. The planner uses the registry entry selected
by `modeling.model`, reusing Model's loaded backend. The existing standalone text
helper and speech demonstration remain available. With speech enabled,
`demo:=true` also echoes the transcript after the selected Look action succeeds.

Planning uses Outlines 1.3.3 directly:
[`from_transformers`](https://dottxt-ai.github.io/outlines/latest/features/models/transformers/)
wraps the already loaded causal text model/tokenizer, and
[`get_json_schema_logits_processor`](https://dottxt-ai.github.io/outlines/latest/api_reference/backends/)
with the `outlines_core` backend creates a fresh token mask for each request.
The mask is passed to the existing Transformers `generate()` call with
`do_sample=False`; prompt/chat formatting and unconstrained generation remain
unchanged. After decoding, `jsonschema` validates the parsed JSON against the
original schema before the response string is returned. Truncated or invalid
responses raise an error; no unrestricted retry or action dispatch occurs.
This path runs on CPU and loads no duplicate model weights.

The current Ollama
backend is not configured for this constrained path and fails explicitly if
selected for planning. The simulation must supply `simulation_actions` and both
action servers. Verify the interface in the simulation environment:

```bash
ros2 interface show simulation_actions/action/Look
ros2 action list -t
```

The singular `action` namespace is used by the Python import. Installation now
builds the current upstream generated interface; see **Temporary Webots Integration**
above. Unit tests still use remapped endpoints and an existing generated test
interface for mocked servers; their success alone does not prove Webots motion.

Build and launch from the repository root (model dependencies below still apply):

```bash
cd ~/ros2_ws/src/smpa-ros
source /opt/ros/jazzy/setup.bash
./install.sh
source ../../install/setup.bash

# One complete cycle (default limit comes from pipeline.yaml).
ros2 launch smpa_ros llm.launch.py service:=vision run_name:=scene_experiment \
  config:="$PWD/config/pipeline.yaml" python:="$PWD/.venv/bin/python"

# Three cycles in one run.
ros2 launch smpa_ros llm.launch.py service:=vision run_name:=three_scenes max_cycles:=3 \
  config:="$PWD/config/pipeline.yaml" python:="$PWD/.venv/bin/python"

# Speech-only session, one Listen and echo per cycle.
ros2 launch smpa_ros speech.launch.py run_name:=dialogue max_cycles:=3 demo:=true \
  python:="$PWD/.venv/bin/python"
```

`llm.launch.py` defaults to vision; `speech.launch.py` defaults to speech.
`service:=all` enables both inputs for every cycle. `world_model_root:=/absolute/path`
selects the storage root (default `$PWD/memory`). Audio device, model root,
and audio lock arguments remain supported. Run names are sanitized and retained in metadata.

```text
memory/
├── legacy/                    # historical observations, unchanged
└── runs/
    └── <timestamp>_<run_name>_<uuid>/
        ├── metadata.yaml      # lifecycle, models, configuration, cycle count
        ├── goal.md            # goal shared by every cycle
        ├── observations/
        │   ├── vision/        # timestamped scene descriptions
        │   └── audio/         # captured WAV + transcript folders
        ├── world_model/
        │   ├── iteration_001.md
        │   └── iteration_002.md
        └── state/
            └── cycle_000001/
                ├── sense.json
                ├── model.json
                ├── plan.json
                └── act.json
```

Memory preserves evidence and generated representations; the world model is the
LLM's evolving environment representation. Existing `world_model/` storage was
renamed once to `memory/`; historical files, run names/IDs, metadata contents, and
internal layouts remain unchanged. There is no runtime migration code. New runs
use timestamped names (default `smpa`) and UUIDs; historical runs are never renamed.

Model selection uses existing configuration and registry files:

```yaml
# config/pipeline.yaml
modeling:
  model: qwen3-0.6b
```

```yaml
# models/model_library.yaml, under models:
qwen3-0.6b:
  backend: transformers
  model_id: Qwen/Qwen3-0.6B
  path: llm/qwen3_0.6b
  enable_thinking: false
```

The checkpoint is already provisioned locally. Runtime loading uses local files
only, once per run; no downloads or automatic substitutions occur. `model_root`
selects the root containing that relative checkpoint path. The backend retains
its `.model` and `.tokenizer` for instrumentation. Sense uses a different model
in a separate process, so its VLM instance cannot be shared with this text LLM.
An existing Ollama backend is also supported for registry entries with
`backend: ollama` and an already installed `model_id`; no additional text backend
framework is introduced.

The modeling instructions live in `smpa_ros/model/prompts/default.txt`; generation
settings live in `smpa_ros/model/models/default.json` (512 new tokens). The prompt
requests Entities, Properties, Relations, and Uncertain Information, stable IDs,
and separation of observations from inferences. These are model instructions,
not semantic guarantees. Snapshot publication is atomic and exclusive: previous
iterations cannot be overwritten. A failed/empty generation records an error in
`model.json`, stops the run, and creates no successful snapshot for that cycle.
The record identifies the prompt, source observation files, previous iteration,
output iteration, model identifier, and cycle. Plan receives the Markdown itself.
`plan.json` records the goal, prompt, model identifier, available actions, and
validated selection. `act.json` records the real request, acceptance, received
feedback, numeric ROS result status, server result payload, cancellation details
if needed, and execution success or error.
These action results are kept separate from LLM predictions.

```bash
find memory/runs -path '*/world_model/iteration_*.md' -print -exec cat {} \;
find memory/runs -path '*/state/cycle_*/model.json' -print -exec cat {} \;
find memory/runs -name plan.json -print -exec cat {} \;
find memory/runs -name act.json -print -exec cat {} \;
```


Results are saved after each successful stage. A failed stage stops execution;
previous results remain, and the count advances only after Act succeeds. Metadata
starts as `running`, finishes as `completed`, and records `failed` or `interrupted`
for failures or handled interruptions. Metadata is flushed and atomically replaced.
SIGKILL or power loss cannot finalize metadata and may leave `running`.
New runs never relocate or modify existing observations. There is no runtime
historical migration infrastructure. Synthesized audio is not stored as microphone
input. Visual Jev remains an unchanged third-party project.

The flow can be read in `loop.py`: `initialize_run()` → `run()` →
`finalize_run()`. Its `main()` connects that flow to ROS. Imports for ROS and
speech clients are lazy so other consumers can use the shared `assigned_run()`
validator without importing ROS or starting orchestration. `run_cycle()` remains
the original non-persistent text helper. There is no separate run-storage module.

The loop prints the directory and endpoint prefix
`/run_<uuid_with_underscores>/...`. It uses the existing launcher in
`participants_only:=true` mode to start Sense/Listen/Talk after initialization.
That mode consumes an assignment and never creates a run. Services are stopped
when the loop finishes; they never independently start cognitive cycles.
For explicit service restarts while the owning loop remains active:

```bash
export RUN_DIR=/absolute/path/to/memory/runs/<existing-run>
export RUN_ID=$(python3 -c 'import os,yaml; print(yaml.safe_load(open(os.environ["RUN_DIR"]+"/metadata.yaml"))["run_id"])')
export RUN_PREFIX=/run_${RUN_ID//-/_}
ros2 launch smpa_ros llm.launch.py participants_only:=true service:=speech \
  world_model_dir:="$RUN_DIR" run_id:="$RUN_ID" python:="$PWD/.venv/bin/python"
```

Stop the previous service instance before restarting its endpoint. Standalone
Sense/Listen/Talk nodes likewise require `world_model_dir` and accept `run_id`
ROS parameters. A supplied ID must match metadata; a missing directory is an error.
All launch-managed clients use per-run endpoint remappings, keeping concurrent
sessions isolated. The shared audio lock continues to serialize physical audio.
Manual clients must use the printed endpoint prefix.

```bash
find memory/runs -name metadata.yaml -print -exec cat {} \;
find memory/runs -path '*/vision/*.md' -print
find memory/runs -path '*/state/cycle_*/*.json' -print
find memory/runs -name transcript.md -print -exec cat {} \;
find memory/runs -name recording.wav -print
./test.sh --unit
./test.sh --integration   # optional real local text LLM + speech inference
./test.sh --all
```

The component reference below describes the individual operations. Its standalone
servers need the assigned run parameters and remappings above; launching the loop
creates a new execution session.

## Persistent local model library

The standalone `scripts/download_models.py` manages exactly eleven public models
under `models/llm/`, `models/vlm/`, and `models/coding/`. Existing root model YAML
files and ROS model-loading behavior are unchanged. Library directories and
`models/download_report.json` are Git-ignored, including metadata and partial
downloads. No existing weights were tracked when this library was added.

From the repository root, use the existing environment:

```bash
source .venv/bin/activate
python scripts/download_models.py
```

Run the same command to resume interruptions or retry skipped/failed models.
Previously completed files are size/checksum-verified and reused. The downloader
checks anonymous repository access, pins the exact revision, and downloads root
Hugging Face inference files through `snapshot_download(local_dir=...)`; it omits
alternate checkpoint formats and subfolder copies. Valid matching cache files
are hard-linked where possible, or symlinked across filesystems, without deleting
or copying the existing cache. Preserve that cache when symlinks are used.

Downloads run sequentially, starting with Qwen3-0.6B. Hub progress bars show actual
transfers. The first model also undergoes local-only Transformers config and
tokenizer loading; no GPU or inference is required. Weight checksums are checked
when provided by Hub metadata. Same-size corrupt files are not treated as complete.

Defaults: two retries (three attempts total), a 30-minute deadline per attempt,
20-second metadata/transfer timeouts, a 5 GiB free-space reserve, and a safe stop
below 2 GiB free. Adjust only retry/deadline limits if needed:

```bash
python scripts/download_models.py --retries 1 --timeout 3600
```

Failures do not stop the batch; gated/private/missing repositories are skipped.
Partial files remain resumable. Ctrl+C/SIGTERM records the interrupted attempt and
stops; sudden power loss/SIGKILL cannot save the current attempt, but the last
atomic report remains valid and partial Hub files remain available. Each report
entry includes status, reason, revision, file manifest, and allocated local disk
bytes (linked weights share storage with the cache). A failed/skipped model may
have partial files; only `completed` means verification succeeded.

```bash
./test.sh
git status --short
git check-ignore -v models/llm/qwen3_0.6b/model.safetensors
```

The download-control tests use mocks and require no downloads. Dependencies
`huggingface_hub` and `transformers` are already in the existing `.venv`; nothing
is installed globally. Hugging Face's local-folder metadata enables resumable
[downloads](https://huggingface.co/docs/huggingface_hub/guides/download).

## ROS logging

Sense logs startup, selected model/backend, weight loading, readiness, and request
completion or failure. The loop logs camera/service waits, image and
response arrival, and the saved Markdown path. Timings use a monotonic clock;
neither image bytes nor model weights are logged. Failures and timeouts use ERROR.

Enable native ROS console colors in each terminal before starting nodes:

```bash
export RCUTILS_COLORIZED_OUTPUT=1
```

Jazzy's built-in colors use the terminal default for INFO, yellow for WARN, and
red for ERROR; green INFO is not configurable through this switch. No custom
coloring is added. The VLM loader uses the `dtype` argument supported by the
documented Transformers 4.57+ environment, replacing deprecated `torch_dtype`.

The ROS 2 package remains `smpa_ros`; `ros2 run smpa_ros hello` still logs
`Hello ROS 2!` and exits. The internal `smpa_ros` module provides a
synchronous, single-cycle Sense–Model–Plan–Act text pipeline and a ROS Sense service.

## Architecture

- `sense/`: prompts for extracting observed facts.
- `model/`: prompts for describing world state and uncertainty.
- `plan/`: prompts for proposing actions or requesting missing information.
- `act/`: prompts for translating plans into proposed command descriptions.
- Each component's `prompts/default.txt` is its input template;
  `models/default.json` supplies inference settings (`max_new_tokens`). These
  assets are included in the installed Python package, with no model weights.
- `backends/base.py` defines only `generate(prompt, *, max_new_tokens) -> str`.
  There is no registry, factory, or mandatory inheritance.
- `loop.py` applies the prompts in order and returns all four outputs. A failed
  stage raises immediately. Backend objects are supplied separately for each
  component, allowing different models or reuse of one loaded instance.

The text loop remains unchanged. Sense additionally exposes `/sense`
using `smpa_ros_interfaces/srv/DescribeScene`: one `sensor_msgs/Image` and a prompt
produce `description` and `error` strings. An empty/whitespace prompt uses
`sense/prompts/describe_scene.txt` (`Describe the scene.`). Errors return an empty
description and a nonempty error; clients must check the error field.

The small nested `smpa_ros_interfaces` ament_cmake package is necessary to generate
custom interfaces while keeping `smpa_ros` an ament_python package. Pass both
package paths explicitly to colcon because recursive discovery stops at a package.
No other component services, subscriptions, or robot execution are implemented.

## Backends and independent selection

Construct backend instances explicitly; selecting a backend does not require
editing any component. For example, after provisioning your own local models:

```python
from smpa_ros.backends.ollama import OllamaBackend
from smpa_ros.backends.transformers import TransformersBackend
from smpa_ros.loop import run_cycle

local = OllamaBackend('your-installed-ollama-model')
development = TransformersBackend('/absolute/path/outside/repository/to/model')
outputs = run_cycle(
    'Observation: a cup is on the table. Goal: describe a possible next action.',
    sense=local, model=development, plan=development, act=local,
)
print(outputs)
```

Ollama uses its HTTP API through Python's standard library and requires a running
server with an already installed model. It does not expose internal activations.
Transformers requires optional, separately installed PyTorch and Hugging Face
Transformers. It loads local files only, once per instance, and keeps `model` and
`tokenizer` accessible for future PEFT adapters and model hooks. No training,
adapters, hooks, or activation recording are implemented. The original `TransformersBackend` still handles causal text models.
`TransformersVLM` uses AutoProcessor and AutoModelForImageTextToText for images;
its model and processor remain accessible for future research instrumentation.

Generation uses greedy decoding (Ollama temperature zero and seed zero).
For reproducible experiments, record exact package versions, model revisions or
digests, prompts/configs, hardware, inputs, and outputs; these defaults alone do
not guarantee identical results across platforms. Keep weights outside Git;
common weight formats and a root `weights/` directory are ignored.

API references: [Ollama generation](https://docs.ollama.com/api/generate) and
[Transformers chat templates](https://huggingface.co/docs/transformers/chat_templating).

## Test and run

From the repository root with ROS 2 Jazzy, colcon, and pytest installed:

```bash
./test.sh
```

The script builds in a temporary workspace, sources ROS and the install overlay,
runs tests without downloads or an Ollama server, verifies installed assets from
outside the source tree, and checks the hello node under a timeout. Temporary
files are removed on exit. Set `ROS_DISTRO` for another installed distribution.

For a persistent local build:

```bash
source /opt/ros/jazzy/setup.bash
./install.sh
source ../../install/setup.bash
(cd ../.. && colcon test --paths src/smpa-ros src/smpa-ros/smpa_ros_interfaces \
  --packages-select smpa_ros --return-code-on-test-failure)
(cd ../.. && colcon test-result --verbose)
ros2 run smpa_ros hello
```

Real model inference requires separately provisioned models and is outside the
download-free test suite.

## Run Sense with a real VLM

The initial model is **SmolVLM2-2.2B-Instruct**, selected by the `model_config`
ROS parameter. The central `models/` YAML files register all three requested
models and are installed under `share/smpa_ros/models`. They contain metadata,
not weights. Relative config names resolve there; absolute YAML paths also work.

Hardware settings live only in `config/pipeline.yaml`; model IDs and paths
are unchanged. Missing settings default to `auto`:

```yaml
vision:
  device: auto   # auto, cpu, or cuda
  dtype: auto    # auto, float32, float16, or bfloat16
```

| Device | Automatic precision | Notes |
| --- | --- | --- |
| CPU | float32 | No CUDA libraries or NVIDIA driver required |
| CUDA (`cuda:0`) | float16 | Selected automatically only when PyTorch reports CUDA available |

Set `vision.device: cpu` to force CPU, or `vision.device: cuda` to require CUDA.
Explicit CUDA fails clearly when unavailable; it never silently falls back.
Explicit float32 works on either device. CPU float16 is rejected because of
limited operation support and poor performance; CPU bfloat16 is accepted with a
warning about hardware/operator support. CUDA bfloat16 requires compatible
hardware. Image tensors use the selected floating dtype; integer token IDs and
masks remain integer. Existing explicit dtype ROS parameters still override YAML
defaults; standalone Sense also accepts `-p device:=cpu` or `-p device:=cuda`.
These are Sense parameters, not new launch arguments.

From the repository directory, rebuild once with the installer, then run:

```bash
./install.sh
./run_smpa.sh                  # Automatic selection; CPU on CPU-only machines
# With the default auto configuration, force CPU for this process:
CUDA_VISIBLE_DEVICES="" ./run_smpa.sh
```

Alternatively set `vision.device: cpu` in the existing YAML and use
`./run_smpa.sh` normally. For CUDA, set `vision.device: cuda` and leave the GPU
visible. Startup prints `Loading vision model on cpu with float32` (or the
selected CUDA combination); Sense logs the actual loaded device and dtype.

On a fresh installation without an existing PyTorch package, `install.sh`
prefers the official CPU wheel index when no working NVIDIA GPU is detected
through `nvidia-smi`. Existing PyTorch installations are preserved, including
CUDA builds; those can also run CPU inference. If GPU drivers are installed
later, provision a compatible CUDA PyTorch build explicitly.

The 2.2B model needs roughly 8.8 GB for float32 CPU weights or 4.4 GB for float16
GPU weights, plus activations, image processing, KV cache, and allocator memory.
Allow additional RAM for the text model and ROS processes. CPU inference can be
slow, particularly for large images or long outputs; the loop's existing waits
can time out on slow hardware. Images are reduced proportionally to at most
512×512 before processing, with 128 output tokens by default. Processor tiling
can still increase memory use. No quantization, model switching, or memory
fallback is introduced.

A local CPU verification with the existing SmolVLM2-2.2B weights, float32,
two CPU threads, a 64×64 solid-red image, and 24 output tokens succeeded:
about 98 seconds of inference and 9.84 GiB peak process RSS. These measurements
are specific to that machine and input, not a bound for normal camera frames.
The backend correctly described the red image; a full ROS/simulation cycle was
not exercised by this check.

The 4B registrations are alternatives, not claims that they fit 8 GB in FP16.
Gemma requires accepting its Hugging Face license and normally uses BF16
(`-p dtype:=bfloat16`), which requires appropriate hardware/operator support. All `vram_gb` values
remain null because requirements have not been measured here.
Model references: [SmolVLM2](https://huggingface.co/HuggingFaceTB/SmolVLM2-2.2B-Instruct),
[Qwen3-VL](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct),
[Gemma 3](https://huggingface.co/google/gemma-3-4b-it).

After installation, a standalone Sense server can use the existing virtual
environment and downloaded weights (the loop normally starts it automatically).
Supply the assigned run variables described earlier:

```bash
source /opt/ros/jazzy/setup.bash
source ../../install/setup.bash
.venv/bin/python -c 'from smpa_ros.sense.service import main; main()' --ros-args \
  -p world_model_dir:="$RUN_DIR" -p run_id:="$RUN_ID" -r /sense:="$RUN_PREFIX/sense" \
  -p config:="$PWD/config/pipeline.yaml" -p device:=cpu -p dtype:=auto
```

Runtime loading is local-only and reuses the existing download report or cache;
there is no second model download. Record dependency versions and checkpoint
revisions for reproducible experiments.

In another terminal at the repository root, send a single red pixel (valid for
checking the request path, though not a meaningful scene):

```bash
source /opt/ros/jazzy/setup.bash
source ../../install/setup.bash
ros2 service call "$RUN_PREFIX/sense" smpa_ros_interfaces/srv/DescribeScene "{image: {height: 1, width: 1, encoding: rgb8, is_bigendian: 0, step: 3, data: [255, 0, 0]}, prompt: ''}"
```

Supported ROS encodings: rgb8, bgr8, rgba8, bgra8, mono8 (including row padding).
The automated suite uses a mock VLM with a real ROS service round trip, checks
prompt defaults, custom prompts and malformed images, and performs no downloads.
Ctrl+C stops the manual service; automated service nodes are destroyed in test
cleanup. Actual VLM quality and GPU memory use require a manual run.

## Camera observations within SMPA cycles

`config/pipeline.yaml` selects `/tiago/camera/color/image_raw`, `smolvlm2`, and the
`describe_scene` prompt. Both the Sense server and loop receive the same config.
The source config can be selected through the `config` launch argument; otherwise
the installed config is used. Rebuild after editing installed configuration assets.

Each cycle waits for Sense, receives one frame with sensor-data QoS, requests its
description, saves it under the run's `observations/vision/` directory, and continues through
Model, Plan, and Act. The response text is saved verbatim. `capture_once()` remains
available for callers with an explicitly assigned run. Errors stop the run;
`--timeout` defaults to 120 seconds per camera/service waiting phase.

```bash
ros2 topic list -t --no-daemon
ros2 topic type /tiago/camera/color/image_raw --no-daemon
ros2 run smpa_ros loop --config "$PWD/config/pipeline.yaml" --run-name camera_run --max-cycles 3
```

The camera must publish `sensor_msgs/msg/Image`. The loop manages the Sense server
as a separate process, using `--python` (default: the loop's interpreter). Run with
an interpreter providing the optional inference dependencies; runtime model
loading remains local-only. No actual camera or VLM inference is performed by the
model-free tests. World models are Markdown snapshots; selected Look actions execute through the simulation action servers.

### Registered model downloads

A download method already exists (`hf download`); no extra downloader is needed.
After installing the optional dependencies described above, attempt all three
sequentially, continuing if a network, storage, or license error occurs:

```bash
for model in Qwen/Qwen3-VL-4B-Instruct HuggingFaceTB/SmolVLM2-2.2B-Instruct google/gemma-3-4b-it; do
  hf download "$model" || echo "Download failed: $model" >&2
done
```

Use the existing default Hugging Face cache outside Git. Gemma may require
license acceptance and `hf auth login`. No automatic model substitution occurs.
The download-free `./test.sh` now also sends a mock camera frame through `/sense`
and verifies the saved Markdown bytes, plus failure behavior with no output file.
Actual camera/VLM inference requires the hardware, ROS camera publisher, and
cached weights; mock tests do not establish VLM quality or GPU memory fit.

### Local speech actions (CPU)

`/speech/listen` uses Faster-Whisper; `/speech/talk` uses Piper. Models load once
per server, independently of ROS in `backends/speech.py`. Their local paths are
registered in `models/model_library.yaml`; downloaded weights are ignored by Git.
Existing vision configuration and interfaces are unchanged.

From the repository root, install the optional audio dependencies and models:

```bash
sudo apt-get install libportaudio2
python3 -m venv --system-site-packages .venv  # if .venv does not already exist
.venv/bin/python -m pip install -r requirements-speech.txt
.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download('Systran/faster-whisper-base',
                  local_dir='models/speech/faster-whisper-base',
                  allow_patterns=['model.bin', '*.json', 'vocabulary.*'])
PY
.venv/bin/python -m piper.download_voices en_US-lessac-medium --download-dir models/speech/piper
source /opt/ros/jazzy/setup.bash
./install.sh
source ../../install/setup.bash
ros2 launch smpa_ros speech.launch.py python:="$PWD/.venv/bin/python"
```

The launch defaults to OS audio devices. Set `input_device:=...` and
`output_device:=...` to a PortAudio device name or index; `model_root:=...` selects
an absolute local models directory. Run launch from this repository root by
default. Neither server downloads models at startup.

In another terminal, source ROS and the same install, then run goals sequentially:

```bash
source /opt/ros/jazzy/setup.bash
source ../../install/setup.bash
ros2 action send_goal "$RUN_PREFIX"/speech/listen smpa_ros_interfaces/action/Listen "{language: en, max_duration: 10.0}" --feedback
ros2 action send_goal "$RUN_PREFIX"/speech/talk smpa_ros_interfaces/action/Talk "{text: 'Hello, I am ready.'}" --feedback
ros2 run smpa_ros listener_talker --ros-args -r /speech/listen:="$RUN_PREFIX/speech/listen" -r /speech/talk:="$RUN_PREFIX/speech/talk"
```

Alternatively add `demo:=true` to launch. The demo awaits each terminal action
result before sending the next goal and says `I heard {transcription}`. Ctrl+C
cancels its active goal. Both servers share an exclusive audio lock, so even
independent clients cannot listen and talk simultaneously. Use the same
`audio_lock` launch argument for servers that share physical audio devices.

Listening starts capture on acceptance, uses a simple RMS speech threshold, and
stops after 0.8 seconds of silence or the requested duration (at most 30 seconds
by default). Empty speech, device errors, and recording timeouts abort the goal.
This threshold may need adjustment in noisy rooms through the listener's
`speech_threshold` and `silence_seconds` ROS parameters. Feedback reports
waiting/recording/transcribing or synthesizing/playing; playback progress measures
submitted audio frames and reaches 1 only after playback drains. Cancellation
aborts an open audio stream promptly. Native model decoding/synthesis can only
observe cancellation between segments/chunks; the audio lock stays held until
that work returns. Canceled results have the ROS canceled state and success=false.

```bash
./test.sh                 # isolated build and hardware/model-free tests
./test.sh --integration   # local CPU models, prerecorded speech, simulated devices
./test.sh --all           # both suites
```

The integration test generates a prerecorded utterance with Piper, captures it
through a simulated input device, transcribes it with Faster-Whisper, and runs the
demo response through Piper and a simulated output device. This tests genuine
inference without microphones, speakers, network access, or GPU resources.

### Persistent Listen observations

Every successful Listen action now writes both microphone audio and recognized
text before returning success:

```text
memory/runs/<run>/observations/audio/2026-10-20T012400_123456Z_<unique suffix>/
├── recording.wav
└── transcript.md
```

The UTC timestamp is generated at capture completion; the suffix guarantees
exclusive session directories. `recording.wav` contains exactly the float32
samples passed to Faster-Whisper: 16,000 Hz, one channel, uncompressed IEEE
float32 PCM in a standard WAV container (format tag 3). Float WAV avoids integer
quantization; use a float-WAV-capable player such as ffplay. Python's standard
`wave` reader supports integer PCM only, so it cannot read this float format.
The linked Markdown records the language, model, format, UTC timestamp, status,
and actual transcription. Audio is written and flushed before transcription;
Markdown is atomically replaced and flushed before action success. Transcription
failures retain audio with `Status: incomplete`. Cancellation during transcription
also retains an incomplete observation; cancellation before completed capture
creates no observation. Storage errors abort the action rather than reporting
successful persistence. Talk audio is never stored as a microphone observation.

The loop assigns the run directory through `world_model_dir` and `run_id`
parameters. See the run-specific storage instructions above for standalone
participants and endpoint remappings. Both vision and Listen share that run for every cycle.

After sourcing the build and starting the speech servers as above:

```bash
ros2 action send_goal "$RUN_PREFIX"/speech/listen smpa_ros_interfaces/action/Listen \
  "{language: en, max_duration: 10.0}" --feedback
find memory/runs -name transcript.md -print -exec cat {} \;
find memory/runs -name recording.wav -print
# Set this to one of the printed paths to hear the preserved capture:
# ffplay -nodisp -autoexit memory/runs/<run>/observations/audio/<session>/recording.wav
./test.sh
./test.sh --integration
./test.sh --all
```

Tests use temporary output roots. They verify exact audio samples, separate
sessions, linked transcripts, incomplete/canceled observations, and storage error
reporting. The prerecorded CPU integration test also decodes the saved WAV and
compares it with input samples while checking the real recognized text. No audio
hardware is required for these tests.
