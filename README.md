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
`outlines==1.3.3`, `jsonschema==4.26.0`, `openai==3.39.0`, and
`python-dotenv==1.2.4`. The setuptools upper bound matches
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

### API inference (text and vision)

Local defaults are unchanged. To use API inference, edit the existing sections
in `config.yaml`, preserving the other settings:

```yaml
vision:
  backend: api
  provider: nebula
modeling:
  backend: api
  provider: nebula
```

Sense and Model/Plan can be selected independently; Model and Plan share one
text backend. For local vision with API planning, set only `modeling.backend:
api`. Restore local vision by removing `vision.backend` (or setting it to
`transformers`); restore local text by removing `modeling.backend`. Local model
names remain in the pipeline but are ignored for stages using the API.

Each API stage selects `provider: openai` or `provider: nebula` independently;
omitting `provider` selects OpenAI. Plan shares the modeling provider. Local
inference remains the default when `backend: api` is absent.

`config.yaml` contains the named provider sections with endpoint, separate
text/vision models, credential variable, and timeout. OpenAI uses its `/v1` base;
Nebula may specify the full `/api/chat/completions` endpoint, which the backend
normalizes before the SDK appends `/chat/completions`. Existing Nebula model
identifiers are preserved. A custom configuration contains both the pipeline
settings and provider sections in the same file. Create `.env` yourself in the
repository root:

```dotenv
OPENAI_API_KEY=your_api_key_here
NEBULA_API_KEY=your_nebula_key_here
```

`.env` is Git-ignored; never put credentials in YAML, URLs, or run records.
Existing environment variables take precedence. Credentials are loaded only
when an API backend is instantiated. Missing credentials fail explicitly.

```bash
cd ~/ros2_ws/src/smpa-ros
./install.sh
./run_smpa.sh max_cycles:=1 timeout:=240
./run_smpa.sh max_cycles:=3 timeout:=240
```

Installation skips all local weight downloads when both stages use API inference;
mixed configurations download only their local models. `--all-models` explicitly
overrides this. The installer still installs dependencies for existing local
backends and builds ROS/simulation interfaces. Installation makes no API requests
and does not establish provider access. The simulation and camera prerequisites
still apply. API inference requires internet access, may incur charges, and sends
images and prompts (including world-model context) to the configured provider.

The official OpenAI SDK uses Chat Completions with PNG image data URLs,
`max_completion_tokens`, and temperature zero. Planning requests strict server-side
`json_schema` output and validates JSON locally; there is no JSON-mode or
unrestricted fallback. Compatible providers must support these features with the
selected models. Refusals, truncated output, and invalid schemas/responses fail
explicitly; automatic request retries are disabled. Local Transformers/Outlines
and Ollama behavior is unchanged.

For authentication failures, check the configured variable and model access; for
429 errors check quota/rate limits. For timeouts, check connectivity and increase
`config.yaml`'s `timeout` if necessary, keeping the launch `timeout` longer
than the API request timeout (180 seconds by default). Truncated responses require
adequate existing stage output-token limits. Unsupported strict schemas or image
inputs require a compatible provider/model; SMPA does not weaken constraints.
API errors are sanitized before logging. Provider verification results and simulation availability are reported separately;
a successful API response alone does not establish completion of a robot action.

Nebula verification: authenticated text, the existing Model/Plan prompts, and
strict-schema action selection succeeded, including a prompt requesting non-JSON
output. Image requests were accepted, but the configured vision model described
both solid-red and solid-blue images as white; image grounding remains unverified.
Do not treat endpoint acceptance as evidence of reliable vision. Full robot-cycle
verification requires a running camera and simulation action servers.

### Models and backend prerequisites

Installation reads `config.yaml` and the existing model YAML files.
Local Transformers vision needs the configured vision weights. API vision does
not need local weights. Transformers
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
./run_smpa.sh config:=/absolute/path/to/config.yaml
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
└── smpa-ros/
```

No vendoring is necessary. `install.sh` explicitly builds the upstream
`simulation_actions` package alongside SMPA, generating Python modules and native
ROS type support under the workspace-level `install/`. It reconfigures an old
CMake cache if it points at a different checkout. This fixes the observed missing
`Look` import: an older copy from another project's `external/` directory had
produced the stale installation. No duplicate interface package is vendored in SMPA.
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

Keep the upstream-source build/preflight blocks in `install.sh`/`run_smpa.sh`
until a replacement action interface is provided. The current fixed Look
registry/executor still requires generated `simulation_actions`; replacing that
robot-control design is a separate task.

## SMPA execution and run storage

`smpa_ros/smpa.py` defines the goal, experiment count, cycle boundaries, prompts,
and stage order. Edit its `run()` function to repeat, skip, or reorder stages.
`Runner` in `runner.py` configures and wires the stages, starts and cleans up ROS
participants, and executes individual stage calls. `MemoryManager` in `memory.py` creates run
directories and goals, tracks completed cycles, and finalizes metadata. Stage
artifacts remain owned by Sense, Model, Plan, and Act. Launch files only start and
configure processes; services and action callbacks do not create runs.

```yaml
# config.yaml
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

The goal is initialized from `goal` in `config.yaml`; it is never autonomously
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

# One complete cycle (default limit comes from config.yaml).
ros2 launch smpa_ros llm.launch.py service:=vision run_name:=scene_experiment \
  config:="$PWD/config.yaml" python:="$PWD/.venv/bin/python"

# Three cycles in one run.
ros2 launch smpa_ros llm.launch.py service:=vision run_name:=three_scenes max_cycles:=3 \
  config:="$PWD/config.yaml" python:="$PWD/.venv/bin/python"

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
# config.yaml
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

The modeling instructions live in `smpa_ros/prompts/model/default.txt`; generation
defaults live in `config.yaml` under `stage_settings.model` (512 new tokens),
with the existing `modeling.max_output_tokens` override taking precedence. The prompt
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

Use `with Runner(...) as runner` for failure and interruption cleanup. Each
`runner.start(goal=GOAL)` creates an independent run; `runner.finish()` closes it.
`with runner.cycle():` counts a completed cycle only after its body succeeds,
regardless of how many stages it contains. Stage calls require this explicit cycle.
The installed `smpa` executable targets `smpa_ros.smpa:main`; both root-level
infrastructure modules are installed and work outside the source checkout.

Select prompt filename stems with `runner.sense(prompt='describe_scene')`,
`runner.model(observation, prompt='default')`, `runner.plan(world, prompt='default')`,
and `runner.act(action, prompt='default')`. Sense's `prompt=None` preserves the
configured vision prompt. Missing prompts fail clearly. Act records its prompt;
ROS dispatch still uses the validated action and existing default goal parameters.
The goal is defined in `smpa.py` and persisted by MemoryManager to `goal.md`.

First invocations retain `sense.json`, `model.json`, `plan.json`, `act.json`, and
`iteration_001.md`. Repeats use `_002`, `_003`, etc. within the same cycle, including
`iteration_001_002.md`. Records carry a run-wide sequence, invocation number,
timestamp, previous-record link, prompt name, and input source. Model uses the
latest successful snapshot even across skipped cycles. Explicit external inputs
have no source record; identical output strings refer to the most recent matching
invocation. No stage implicitly invokes another stage. Plan still requires a
world-model string, and Act still requires a supported action.

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

The ROS 2 package remains `smpa_ros`. Speech lives in the root-level `speech/`
Python package and retains its ROS entry points. The `smpa_ros` module provides a
synchronous, single-cycle Sense–Model–Plan–Act text pipeline and a ROS Sense service.

## Architecture

- `sense/`: prompts for extracting observed facts.
- `model/`: prompts for describing world state and uncertainty.
- `plan/`: prompts for proposing actions or requesting missing information.
- `act.py`: ROS action execution and result persistence; prompts live in `prompts/act/`.
- Each stage's `prompts/<stage>/default.txt` is its input template.
  `config.yaml` supplies independent `stage_settings.<stage>.max_new_tokens`
  defaults: Sense 128, Model 512, Plan 128, Act 128. Existing Model/Plan
  `max_output_tokens` overrides retain precedence. Prompt assets are installed
  with the Python package; configuration is installed under `share/smpa_ros`.
- `backends/base.py` defines only `generate(prompt, *, max_new_tokens) -> str`.
  There is no registry, factory, or mandatory inheritance.
- `smpa.py` calls Sense, Model, Plan, and Act in order and propagates failures.
  Runner exposes each stage independently and shares the configured text backend.

The text loop remains unchanged. Sense additionally exposes `/sense`
using `smpa_ros_interfaces/srv/DescribeScene`: one `sensor_msgs/Image` and a prompt
produce `description` and `error` strings. An empty/whitespace prompt uses
`prompts/sense/describe_scene.txt` (`Describe the scene.`). Errors return an empty
description and a nonempty error; clients must check the error field.

The small nested `smpa_ros_interfaces` ament_cmake package is necessary to generate
custom interfaces while keeping `smpa_ros` an ament_python package. Pass both
package paths explicitly to colcon because recursive discovery stops at a package.
No other component services, subscriptions, or robot execution are implemented.

## Backends and independent selection

The configured Model backend is shared by Model and Plan. Select it in
`config.yaml`; prompt selection and cognitive sequencing belong in `smpa.py`.
Plan retains its strict schema requirement, so Ollama remains usable for Model
but cannot execute schema-constrained Plan with the current backend.

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
outside the source tree, and checks the installed Speech entry points. Temporary
files are removed on exit. Set `ROS_DISTRO` for another installed distribution.

For a persistent local build:

```bash
source /opt/ros/jazzy/setup.bash
./install.sh
source ../../install/setup.bash
(cd ../.. && colcon test --paths src/smpa-ros src/smpa-ros/smpa_ros_interfaces \
  --packages-select smpa_ros --return-code-on-test-failure)
(cd ../.. && colcon test-result --verbose)
ros2 run smpa_ros smpa --help
```

Real model inference requires separately provisioned models and is outside the
download-free test suite.

## Run Sense with a real VLM

The initial model is **SmolVLM2-2.2B-Instruct**, selected by the `model_config`
ROS parameter. The central `models/` YAML files register all three requested
models and are installed under `share/smpa_ros/models`. They contain metadata,
not weights. Relative config names resolve there; absolute YAML paths also work.

Hardware settings live only in `config.yaml`; model IDs and paths
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
.venv/bin/python -c 'from smpa_ros.sense import main; main()' --ros-args \
  -p world_model_dir:="$RUN_DIR" -p run_id:="$RUN_ID" -r /sense:="$RUN_PREFIX/sense" \
  -p config:="$PWD/config.yaml" -p device:=cpu -p dtype:=auto
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

`config.yaml` selects `/tiago/camera/color/image_raw`, `smolvlm2`, and the
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
ros2 run smpa_ros smpa --config "$PWD/config.yaml" --run-name camera_run --max-cycles 3
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
per server, independently of ROS in `speech/backends.py`. Their local paths are
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

## Local conversational GUI

The conversational entry point runs an epistemic **Sense → Act** loop: receive
and save a user message, then generate and save a response. New conversations
default to **Qwen/Qwen3-0.6B**, using the existing local Transformers backend and
registry entry `qwen3-0.6b`. This GUI default is independent of the robot pipeline
in `config.yaml`, which is unchanged. No API credentials or internet connection
are required for local inference once the weights and dependencies are installed.
It does not initialize ROS, call Model/Plan, or execute robot actions. No camera,
speech models, Webots, or action servers are required to run the GUI.

From the checkout, reuse the existing environment (no new dependencies):

```bash
cd ~/ros2_ws/src/smpa-ros
# Install/update the Python package and its packaged HTML in the existing environment:
.venv/bin/python -m pip install --no-deps .
# Launch directly; sourcing ROS is unnecessary for the conversational application:
.venv/bin/python -m smpa_ros.conversation_web --config "$PWD/config.yaml"
```

For a fresh full robotics installation, `./install.sh` remains the existing setup
command with its documented prerequisites. The Python-only installation command
above assumes that environment and the selected inference dependencies already
exist; it does not provision local weights. API inference requires the configured
credential in `.env` or the environment and sends conversation history to that
provider. No live API request is made until a message is sent.

Open **http://127.0.0.1:8000** in a browser, or on Ubuntu:

```bash
xdg-open http://127.0.0.1:8000
```

The conversation header has an **Active agent** selector, populated from the
existing text-model registry and API provider sections in `config.yaml`.
Entries distinguish Local/Transformers (or Local/Ollama) from API providers.
Selecting an agent is saved immediately without loading its model or discarding
history; its backend initializes on the first message. API inference requires
explicit selection for a new run. Missing local weights or credentials produce
an advisory in the GUI and a failed message if inference cannot run; there is
no automatic cloud fallback and no automatic model download.

An agent is an inference configuration: provider, backend, model, generation
settings, local path or API endpoint, and model-specific options. Its stable ID
includes a configuration hash. Generation limits reuse the existing `modeling`
and `stage_settings.model` settings; API reasoning controls are omitted for
local models. Backend sampling defaults remain unchanged. The selected snapshot
is stored as `active_agent` in run metadata. Each assistant response records its
agent snapshot in both `conversation.md` (a length-delimited HTML comment) and
its `act.json`; the GUI displays provider/model attribution below the reply.
Credentials are never included in these agent snapshots.

Resuming restores the saved selection, including an explicitly selected API
agent. Older runs without `active_agent` restore their recorded `models.text`
selection when available; old responses are marked as lacking per-response
attribution rather than retroactively assigning one. Changes to an existing
agent's configuration require the original settings to resume reproducibly.
Restart after editing configuration; changes during inference are rejected.

The left panel supports Enter-to-send and Shift+Enter for newlines. The right
panel is an empty belief graph; there are no generated beliefs or placeholder
entities. Its frontend receives an empty `{nodes: [], edges: []}` graph object
for a future Model stage. No graph library or frontend build is needed yet.

Each server process owns one conversation. Refreshing the page reloads its
history. The server prints the run directory under `memory/runs/`; it contains
`goal.md`, `metadata.yaml`, `conversation.md`, and the standard MemoryManager
subdirectories. `state/cycle_*/sense.json` and `act.json` record each exchange.
The transcript is append-only Markdown with hidden byte-length comments that
allow exact reloading even when messages contain Markdown role headings.
Incomplete or malformed transcripts are rejected and preserved unchanged.

Stop with Ctrl+C. To continue a saved conversation, use its printed directory:

```bash
.venv/bin/python -m smpa_ros.conversation_web --config "$PWD/config.yaml" \
  --resume "$PWD/memory/runs/REPLACE_WITH_RUN_DIRECTORY"
```

Resuming requires the original configuration and rejects robot runs. An exclusive
file lock prevents two application processes from writing the same transcript.
The server listens only on `127.0.0.1`; use `--port 8001` if necessary. Exchanges
are serialized. All saved messages are included in the inference prompt; long
conversations remain subject to the selected model's context limit, with no
silent truncation. Failed inference keeps the user's message and prior history,
records failure, and allows the next exchange. Shutdown finalizes run metadata;
process exit releases inference resources.

Run the mocked conversation tests without ROS, API access, or models:

```bash
.venv/bin/python -m unittest discover -s test -p test_conversation.py -v
```

Run the existing build/unit suite, which also discovers these tests and verifies
the installed conversation entry point and HTML:

```bash
./test.sh --unit
```

### Conversational local-model requirements

The supported Qwen3-0.6B checkpoint is already present in
`models/llm/qwen3_0.6b` in this checkout (approximately 1.5 GiB on disk).
The existing Transformers text backend runs on CPU without requiring CUDA.
An offline smoke check with a short greeting and a 16-token output limit used
about **4.94 GiB peak process RSS**. Allow **6–8 GiB available RAM** as a starting
point; dependency loading, longer histories, KV cache, and other loaded models
can increase memory use. This is a measured short-run result, not a hard bound.
No GPU memory is required by this CPU backend.

Reuse the existing `.venv` and weights. If the checkpoint is missing, download
it explicitly using the repository's existing downloader (this provisioning
step requires internet, unlike subsequent local inference):

```bash
cd ~/ros2_ws/src/smpa-ros
.venv/bin/python scripts/download_models.py --model Qwen/Qwen3-0.6B
```

The downloader retains its 5 GiB free-disk reserve. It is never invoked by the
GUI. The full environment installation remains `./install.sh`; updating only
the GUI in an existing environment uses the `pip install --no-deps .` command
above. No new runtime dependencies were introduced for agent selection.

### Conversational reasoning traces

Conversational inference returns a final `response` and, only when explicitly
provided by the backend, a separate `thinking` field. Supported sources are:

- OpenAI-compatible API responses with plain-text `reasoning_content` or
  `reasoning` fields. Token counts, encrypted objects, and absent fields do not
  produce traces; no separate request is made to explain an answer.
- Ollama's explicit `thinking` response field.
- Local Qwen3's documented leading `<think>…</think>` block. These delimiters
  are interpreted only for the Qwen3 model family; other models' literal markup
  is not reclassified as reasoning. Incomplete thinking blocks or missing final
  answers fail instead of displaying reasoning as the answer.

This captures emitted reasoning; it does not enable reasoning, change prompts,
or alter generation defaults. In particular, the registry's local Qwen default
still has `enable_thinking: false`. Providers that do not expose a reasoning
trace continue to return and persist only the response. Model and Plan keep
using the existing string-returning inference path; only the conversation opts
into structured results through the shared backend loader.

New assistant transcript entries use `<response>` tags and include a preceding
`<thinking>` section when a trace exists. Byte lengths in the existing message
marker delimit both sections, so literal tags or headings within model output
cannot break the association. Old transcripts are read unchanged; old literal
reasoning-looking tags are not interpreted as new traces.

Each trace shares its response's agent provenance. If an API or Ollama response
reports a different actual model identifier, that identifier is recorded along
with the requested model. The corresponding Act record stores `response`, optional
`thinking`, and its backend field/delimiter source. Run selection remains unchanged.

The GUI displays the final response normally and a native, keyboard-accessible
**Thinking** disclosure, collapsed by default, for available traces. Both parts
are rendered as text. Message JSON exposes typed `parts` (`user`, `thinking`,
`response`) for future filtering. Subsequent inference receives the chronological
user/final-response history, not prior thinking traces.

Run mocked conversation tests, including backward compatibility:

```bash
.venv/bin/python -m unittest discover -s test -p test_conversation.py -v
```

Run the full regression suite and the optional real-browser expand/collapse
check using an existing Firefox installation (no browser dependency is added):

```bash
SMPA_BROWSER_TEST=1 ./test.sh --unit
```

Without the environment flag or Firefox, the browser test is skipped; the other
unit tests still run with `./test.sh --unit`. No live API or Ollama service is
needed for these tests.
