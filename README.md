# Minimal robot cognition

## Visual Jev: image → forced-choice probabilities

`smpa_ros` now exposes `/visual_jev/evaluate` using
`smpa_ros_interfaces/srv/EvaluateVisualDecision`. The request carries an image,
public context, a question, and 2–16 distinct nonempty choices. The response has
success/error fields, labels in request order, probabilities, and the selected
answer. Failures are returned explicitly. The defaults in the one-frame client
ask whether an image contains something interesting, with choices `yes` and `no`.
These are probabilities relative to the supplied choices, not calibrated
interestingness measurements.

The wrapper calls the released `VDM.prepare_group()` and `VDM.run_independent()`
and follows the official example's probability readout. It does not replace the
decision algorithm, generate an intermediate description, train, or merge away
the adapter. `backend.vdm`, `backend.model` (PEFT), `backend.last_output`, and
`backend.last_logits` remain available; transformer blocks and hooks are accessible
through `backend.vdm.vl_model.language_model`.

This checkout previously had no central registry or launch file. The new
`models/model_library.yaml` adds Visual Jev without moving the existing
`llm/`, `vlm/`, or `coding/` directories or changing the existing model YAML files.
Its base and adapter live under `models/vision/visual-jev-4b/`; all model files and
the local third-party checkout are Git-ignored. The downloader reuses compatible
cached/library files by revision rather than duplicating weights.

### Source, dependencies, and model downloads

Run from the repository root. The official code revision inspected for this
integration is pinned below; the source is not copied into this ROS package.

```bash
git clone https://github.com/guanxuyu-sv/Visual-Jev.git third_party/Visual-Jev
git -C third_party/Visual-Jev checkout a392eaedfa41adae348f9767b55c0f54b3a13b57
source .venv/bin/activate
python -m pip install --no-deps peft==0.21.0
python scripts/download_models.py --visual-jev
```

If the checkout or dependency already exists, reuse it. The existing environment
already supplies PyTorch, Transformers, Pillow, Accelerate, and Safetensors;
PEFT was the only missing inference dependency. Full upstream reproduction pins
are in `third_party/Visual-Jev/code/requirements.txt`; do not replace a working
CUDA installation just to install benchmark/data-analysis dependencies.
Both downloads completed: base revision `ebb281ec70b05090aa6165b016eac8ec08e71b17`
and adapter revision `7a3f1bb0d8f7bfbbd8514d968ab7de3518849c8d`. Re-running the
downloader resumes/verifies these files. The initial eleven-model batch remains
the default when `--visual-jev` is omitted.

### Hardware and loading

The released loader accepts device/dtype but no quantization configuration. The
official quickstart advertises CUDA/MPS; the underlying VDM implementation also
accepts CPU tensors. This wrapper offers explicit CPU float32 or CUDA BF16, with
no automatic substitution or fallback. It checks available memory before loading:
32 GiB available RAM for CPU, or 9 GiB free VRAM for CUDA (conservative headroom
over the project's reported 8.4 GiB one-question BF16 peak). These guards are not
guarantees for arbitrary prompt lengths. Inputs default to the released 200704
pixel budget. Neither GGUF nor an unvalidated quantized adapter path is provided.

The development GPU was identified as an RTX 3070 Laptop, 8192 MiB total and
2576 MiB free at inspection. It does not meet this CUDA loading guard. CPU
available RAM was approximately 49 GiB. CUDA availability differs inside the
sandbox, so no GPU inference or Visual Jev GPU peak memory was measured here.
The real CPU integration run initialized in 8.29 seconds, reached a peak process
RSS of 26287.5 MiB (25.7 GiB), and returned probabilities `[0.83986, 0.16014]`
for `[yes, no]` on the official bottle photograph through ROS transport.
The 32 GiB CPU guard includes headroom over that measured process peak.

### Build, launch, request, and capture

```bash
source /opt/ros/jazzy/setup.bash
colcon build --paths . ./smpa_ros_interfaces --packages-select smpa_ros_interfaces smpa_ros
source install/setup.bash
OMP_NUM_THREADS=4 ros2 launch smpa_ros llm.launch.py service:=visual_jev model:=visual-jev-4b device:=cpu python:="$PWD/.venv/bin/python"
```

The new launch selector starts exactly one node. Its default `service:=sense`
preserves the existing Sense behavior; `model:=smolvlm2` selects that Sense YAML.
Visual Jev does not start Sense, Ollama, or another language-model service.
`model_root` and `source_dir` launch arguments override absolute local paths.
For CUDA on a machine meeting the memory guard, explicitly use `device:=cuda`.

In a second terminal, source ROS and `install/setup.bash`, then send a minimal
one-pixel request to check service communication:

```bash
ros2 service call /visual_jev/evaluate smpa_ros_interfaces/srv/EvaluateVisualDecision "{image: {height: 1, width: 1, encoding: rgb8, step: 3, data: [255, 0, 0]}, context: '', question: 'Does this image contain something interesting that deserves further investigation?', choices: ['yes', 'no']}"
```

For one actual camera frame, use `camera_topic` from the installed pipeline YAML:

```bash
ros2 run smpa_ros visual_jev_camera
# Or supply the topic, arbitrary question, choices, and context:
ros2 run smpa_ros visual_jev_camera --topic /tiago/camera/color/image_raw --question 'Is there a person?' --choices yes no --context 'Robot observation'
```

The client subscribes once, unsubscribes after its first frame, prints the answer
and probabilities, and exits. Stop the service with Ctrl+C.

### Tests

```bash
./test.sh --unit
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 ./test.sh --integration
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 ./test.sh --all
```

Unit tests never initialize/download large models. Integration uses the local
base/adapter and the official bottle photograph, sending it through a ROS image
publisher, service, and one-frame client. It defaults explicitly to CPU; set
`SMPA_VISUAL_JEV_DEVICE=cuda` only for a supported GPU. It skips with a reason if
the adapter or PEFT is missing, and does not download anything. The physical
camera is not required for this reproducible transport test. Executors, nodes,
subscriptions, clients, and temporary build products are cleaned up.
Validation completed here: 36 unit tests passed; the standalone integration run
passed; the combined run passed all 37 tests without skips. The live ROS graph
had no camera topic, so a physical-camera experiment was not executed.

References: [official source and hardware results](https://github.com/guanxuyu-sv/Visual-Jev),
[released adapter](https://huggingface.co/guanxuyu/visual-jev-4b-answer-sft).

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
two files under `world_model/`, then exits:

- `YYYY-MM-DD_HH-MM-SS_microsecondsZ_sense_output.md`: the unchanged VLM response.
- The same basename with `.png`: the received camera frame at its original size,
  converted to RGB and saved losslessly (before Sense's inference resizing).

The filename timestamp is image receipt time; `Z` means UTC. Parse it with
`datetime.strptime(timestamp, '%Y-%m-%d_%H-%M-%S_%fZ').replace(tzinfo=timezone.utc)`.
Service round-trip timing remains available in the ROS logs. Run from the
repository root to use its world_model folder.
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
