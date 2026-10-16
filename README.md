# Minimal robot cognition

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
