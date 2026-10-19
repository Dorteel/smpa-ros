# Minimal robot cognition

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
completion or failure. The one-shot loop logs camera/service waits, image and
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
python -c 'from smpa_ros.sense.service import main; main()' --ros-args -p model_config:=smolvlm2.yaml -p dtype:=float16
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
ros2 service call /sense smpa_ros_interfaces/srv/DescribeScene "{image: {height: 1, width: 1, encoding: rgb8, is_bigendian: 0, step: 3, data: [255, 0, 0]}, prompt: ''}"
```

Supported ROS encodings: rgb8, bgr8, rgba8, bgra8, mono8 (including row padding).
The automated suite uses a mock VLM with a real ROS service round trip, checks
prompt defaults, custom prompts and malformed images, and performs no downloads.
Ctrl+C stops the manual service; automated service nodes are destroyed in test
cleanup. Actual VLM quality and GPU memory use require a manual run.

## One camera frame → Sense → Markdown

`config/pipeline.yaml` is the shared configuration: `camera_topic` is
`/TLO/Camera/Color/ImageRaw`, `model` is `smolvlm2`, and `prompt` is
`describe_scene`. The service uses the model setting; the one-shot loop resolves
that prompt name to `sense/prompts/describe_scene.txt`. Both read the installed
configuration by default. For an edited source configuration, pass the same
absolute file path to both processes as shown below. The existing `model_config`
service parameter can still override model selection explicitly.

The loop waits for `/sense`, subscribes with sensor-data QoS, takes one image,
unsubscribes, requests a description, and saves the response verbatim. It creates
`world_model/YYYY-MM-DD_HH-MM-SS_microseconds_sense_output.md` under the current
directory, then exits. Run from the repository root to use its world_model folder.
Generated outputs are ignored in Git. There is no interpretation or world-state
update. Service errors and timeouts raise without producing a Markdown file.
`--timeout` defaults to 120 seconds per waiting phase; `--output-dir` can override
the destination. The older `run_cycle()` text helper remains unchanged.

After building and downloading the model as above, start Sense from the root:

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash
source /tmp/smpa-vlm/bin/activate
python -c 'from smpa_ros.sense.service import main; main()' --ros-args -p config:="$PWD/config/pipeline.yaml"
```

With VLM dependencies installed in the ROS Python environment, the equivalent is:

```bash
ros2 run smpa_ros sense --ros-args -p config:="$PWD/config/pipeline.yaml"
```

In a second terminal at the repository root:

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash
ros2 topic list -t --no-daemon
ros2 topic type /TLO/Camera/Color/ImageRaw --no-daemon
ros2 run smpa_ros loop --config "$PWD/config/pipeline.yaml"
```

The camera must publish `sensor_msgs/msg/Image`. The loop needs ROS dependencies
but no PyTorch installation; only the Sense process loads the model. To run the
module directly, use `python3 -m smpa_ros.loop --config config/pipeline.yaml`.
Ctrl+C stops the service. Loop subscriptions, clients, executors, and nodes are
cleaned up on completion or error.

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
ros2 action send_goal /speech/listen smpa_ros_interfaces/action/Listen "{language: en, max_duration: 10.0}" --feedback
ros2 action send_goal /speech/talk smpa_ros_interfaces/action/Talk "{text: 'Hello, I am ready.'}" --feedback
ros2 run smpa_ros listener_talker
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
world_model/audio/2026-10-20T012400_123456Z_<unique suffix>/
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

`config/pipeline.yaml` now defines the absolute `world_model_dir`, initially this
repository's existing world_model directory. Both the vision loop (unless
`--output-dir` is supplied) and Listen use it, independent of their working
directory. Update that setting if the repository moves, then rebuild to refresh
the installed configuration. The listener also accepts a `world_model_dir` ROS
parameter override, for example:

```bash
.venv/bin/python -c 'from smpa_ros.speech.actions import listen_main; listen_main()' \
  --ros-args -p world_model_dir:="$PWD/world_model"
```

After sourcing the build and starting the speech servers as above:

```bash
ros2 action send_goal /speech/listen smpa_ros_interfaces/action/Listen \
  "{language: en, max_duration: 10.0}" --feedback
find world_model/audio -name transcript.md -print -exec cat {} \;
find world_model/audio -name recording.wav -print
# Set this to one of the printed paths to hear the preserved capture:
# ffplay -nodisp -autoexit world_model/audio/<session>/recording.wav
./test.sh
./test.sh --integration
./test.sh --all
```

Tests use temporary output roots. They verify exact audio samples, separate
sessions, linked transcripts, incomplete/canceled observations, and storage error
reporting. The prerecorded CPU integration test also decodes the saved WAV and
compares it with input samples while checking the real recognized text. No audio
hardware is required for these tests.
