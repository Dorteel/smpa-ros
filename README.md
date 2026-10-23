# Minimal robot cognition

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
`look_left` (`/look_left`) or `look_right` (`/look_right`). Act sends an empty goal
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

Planning reuses `letssee_llm.structured` and its existing Outlines token masks;
it does not implement a second schema framework. Source the workspace containing
that utility and make its optional constrained-decoding dependencies available to
the selected Python interpreter. Missing support produces a clear Plan error,
with no unrestricted fallback and no ROS action dispatch. The current Ollama
backend is not configured for this constrained path and fails explicitly if
selected for planning. The simulation must supply `simulation_actions` and both
action servers. Verify the interface in the simulation environment:

```bash
ros2 interface show simulation_actions/action/Look
ros2 action list -t
```

The singular `action` namespace is used by the Python import. That interface is
not installed in this checkout's test environment; genuine simulation motion has
not been verified here. Tests use remapped endpoints and substitute a generated
existing test interface for mocked Look servers.

Build and launch from the repository root (model dependencies below still apply):

```bash
cd /home/dorteel/ros2_ws/src/smpa-ros
source /opt/ros/jazzy/setup.bash
source /home/dorteel/ros2_ws/install/setup.bash
colcon build --paths . ./smpa_ros_interfaces --packages-select smpa_ros_interfaces smpa_ros
source install/setup.bash

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
validated selection. `act.json` records the real request, acceptance, numeric ROS
result status, cancellation details if needed, and execution success or error.
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
colcon build --paths . ./smpa_ros_interfaces --packages-select smpa_ros_interfaces smpa_ros
source install/setup.bash
colcon test --paths . ./smpa_ros_interfaces --packages-select smpa_ros --return-code-on-test-failure
colcon test-result --verbose
ros2 run smpa_ros hello
```

Real model inference requires separately provisioned models and is outside the
download-free test suite.

## Run Sense with a real VLM

The initial model is **SmolVLM2-2.2B-Instruct**, selected by the `model_config`
ROS parameter. The central `models/` YAML files register all three requested
models and are installed under `share/smpa_ros/models`. They contain metadata,
not weights. Relative config names resolve there; absolute YAML paths also work.

Use FP16 (`dtype:=float16`) on the single GPU `cuda:0`. Roughly 4.4 GB is needed
for 2.2 billion FP16 parameters alone; activations, image processing, KV cache,
and allocator overhead add memory. This is a starting choice for 8 GB, not a
measured fit guarantee. Images are reduced proportionally to at most 512×512
before processing and output is limited to 128 new tokens by default. Processor
tiling can still increase memory; long prompts can exhaust VRAM. No detection,
offloading, quantization, or automatic fallback is performed.

The 4B registrations are alternatives, not claims that they fit 8 GB in FP16.
Gemma requires accepting its Hugging Face license and normally uses BF16
(`-p dtype:=bfloat16`), which requires a compatible GPU. All `vram_gb` values
remain null because requirements have not been measured here.
Model references: [SmolVLM2](https://huggingface.co/HuggingFaceTB/SmolVLM2-2.2B-Instruct),
[Qwen3-VL](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct),
[Gemma 3](https://huggingface.co/google/gemma-3-4b-it).

Build using the commands above, then install optional inference dependencies in
an isolated environment with access to the system ROS Python packages:

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash
python3 -m venv --system-site-packages /tmp/smpa-vlm
source /tmp/smpa-vlm/bin/activate
python -m pip install torch torchvision 'transformers>=4.57,<5' num2words pillow pyyaml huggingface_hub
hf download HuggingFaceTB/SmolVLM2-2.2B-Instruct
python -c 'from smpa_ros.sense.service import main; main()' --ros-args -p world_model_dir:="$RUN_DIR" -p run_id:="$RUN_ID" -r /sense:="$RUN_PREFIX/sense" -p model_config:=smolvlm2.yaml -p dtype:=float16
```

The explicit `hf download` populates the default cache outside the repository
(`~/.cache/huggingface`). Runtime loading is local-only. The Python invocation
ensures the service uses the virtual environment; with dependencies installed in
the ROS Python environment, `ros2 run smpa_ros sense` is equivalent. No weights
are downloaded by the service or the tests. Pin dependencies and checkpoint
revisions for recorded experiments.

In another terminal at the repository root, send a single red pixel (valid for
checking the request path, though not a meaningful scene):

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash
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
colcon build --paths . ./smpa_ros_interfaces --packages-select smpa_ros_interfaces smpa_ros
source install/setup.bash
ros2 launch smpa_ros speech.launch.py python:="$PWD/.venv/bin/python"
```

The launch defaults to OS audio devices. Set `input_device:=...` and
`output_device:=...` to a PortAudio device name or index; `model_root:=...` selects
an absolute local models directory. Run launch from this repository root by
default. Neither server downloads models at startup.

In another terminal, source ROS and the same install, then run goals sequentially:

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash
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
