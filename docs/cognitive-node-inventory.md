# Independent cognitive nodes: migration inventory

This inventory records the control flow at base commit `2efae36`. It describes
existing responsibilities and the ownership requested for the migration; it is
not a claim that the independent nodes have already been implemented.

| Existing responsibility | Current implementation and dependencies | Intended owner |
|---|---|---|
| Experiment goal, cycle count, stage order and completion | `smpa_ros.smpa.run` calls `Runner.start`, `cycle`, `sense`, `model`, `plan`, `act`, and `finish`; refinement can set `experiment_complete`. | `smpa.py`, using stage action clients and shared run memory. |
| Configuration, prompts, modality selection and frozen run settings | `Runner.configure` combines ROS package resources, command arguments, modality switches and effective generation settings. `MemoryManager` persists `parameters.yaml`. | Each node owns its ROS configuration; the experiment and memory retain the frozen run record and identity. Existing override behavior needs migration tests. |
| Run creation, cycle and provenance | `MemoryManager.__init__`, `begin_cycle`, `invocation`, `complete_cycle`, and `finalize`; `Runner` holds the live instance and invocation-to-source mappings. | Shared Python memory library and explicit run context supplied to stages; experiment owns boundaries and ordering. |
| Sensory participants | `Runner.setup`, `observe`, and `teardown` create an in-process `SenseService`, camera executor and Read node, and launch speech processes. `smpa_ros.sense.observe` invokes enabled Vision, Hear, and Read in order. | Sense node and existing sensor services/actions; their lifecycle must not depend on a shared Runner object. |
| Observation persistence | `smpa_ros.sense.record_observation` records source files and commits a successful Read cursor with the Sense record. `sense.vision` saves image evidence. | Sense node using shared memory; preserve atomic cursor and source provenance. |
| World model | `Runner.model` passes source records and `previous_snapshot` to `smpa_ros.model.generate_world_model`, which writes exclusive Markdown snapshots and records. | Model node; inference goes through the existing LLM actions. |
| Belief and ontology | `smpa_ros.belief.model_messages` validates ontology triples, writes SDS and atomically replaces `belief-state.ttl`; `conversation.py` currently invokes it directly. | Model owns belief modifications; conversation compatibility needs explicit migration, not merely a new robot-stage endpoint. |
| Action selection | `Runner.plan` calls `smpa_ros.plan.select_action`, which reads `goal.md`, constructs the schema, generates and validates JSON, writes a record, and returns an action name. Parameters remain in the record and `Runner.action_parameters`. | Plan node; the action result must carry the complete validated decision needed by Act, including parameters. |
| Robot execution | `Runner.dispatch` and `smpa_ros.act.perform_action` validate decisions, call existing ROS action endpoints, and persist execution details. | Act node; retain existing robot endpoints and result checks. |
| Scene refinement | `Runner.identify_questions` derives an entity/question queue. `focused_observation` calls the in-process Vision service, updates that queue and pending observations; Sense, Plan, Model, and Act use that shared state. | Redistribute sensory operations and model updates to the appropriate nodes without hiding stage sequencing or changing the persisted refinement record. |
| Backend lifetime | `Runner.initialize_backends` and `setup` resolve a shared multimodal model and pass one local backend object to Sense, Model, and Plan. `smpa_llm.server` instead owns one backend per server and serializes its actions. | Existing inference server instances; configure ownership and separate instances explicitly when distinct capabilities or settings require them. |
| Lifecycle and events | `smpa_ros.run_control` starts an experiment child, wraps Runner stage methods, relays a private notification pipe, and handles bounded shutdown. `llm.launch.py` launches the Runner or legacy participants. | Existing lifecycle services start and observe the experiment; launch starts independent stage processes. Neither chooses stage order. |

## Migration risks and required interfaces

- Four stage actions need an explicit run identity and capability selector. Their
  nodes also need a reliable way to resolve that identity under the configured
  memory root and identify the active cycle and invocation. `assigned_run`
  currently validates an absolute directory and optional ID; it does not look
  up a run by ID or reconstruct an active robot run.
- `MemoryManager` keeps cycle, sequence, per-stage counts, previous record and
  metadata in one Python object. Its atomic file replacement prevents partial
  files but does not synchronize read-modify-write operations across processes.
  File locking and durable, shared invocation state are needed before concurrent
  stage processes can safely allocate provenance or update metadata and beliefs.
- Model and Plan currently consume a callable that accepts per-call generation
  settings, while the LLM action goals deliberately omit those settings. ROS
  parameter ownership and explicit server instances must preserve stage-specific
  configuration without changing an in-flight request or silently overwriting
  another stage's settings.
- The current vision service holds a frozen image and refinement state in the
  same process as Runner. Focused observations, cached image identity, Read
  cursors, and pending observation provenance must survive separate processes.
- The existing inference actions reject cancellation for blocking backends.
  Stopping an experiment must not pretend that an ongoing backend operation was
  canceled or unload a model in use.
- `run_control` currently observes stage methods through wrappers around Runner.
  Migrating the experiment needs a replacement event path that reflects actual
  calls while retaining the existing public lifecycle services and cleanup.
- Existing Runner callers include the experiment, lifecycle worker, and multiple
  regression tests. Conversation has a separate belief path. Remove old code
  only after these callers have migrated and compatibility checks pass.

## Migration status after conversation delegation

The explicit experiment sequence and the four stage action servers are present.
The Model action now accepts the `belief` capability and `messages_json`; it
attaches to the active run and calls the existing belief writer. The inference
server exposes `llm/generate_structured_chat` using `GenerateStructuredChat`:
its goal has `messages_json`, optional `json_schema`, and `run_id`, and its
result has `json_output`. An empty schema permits a plain response; a supplied
schema uses the existing backend enforcement and output validation.

Conversation belief updates require a running `model_stage` with `memory_root`
matching the conversation root and `belief_ontology` matching the configured
ontology, plus a loaded `/llm` server with the configured belief model and
generation controls. Each exchange acquires and records its server freeze,
checks it for every Model call, and releases it only after requests terminate.
A server restart or changed snapshot stops continuation rather than replacing
recorded provenance. Conversation replies still use the existing selectable
lazy backend; they may load a separate model in addition to the belief server.

## Generated interfaces and ROS transport verification

The `smpa_ros_interfaces` package was built in `/tmp/cognitive-stage/install`
with one build worker. The generated-action contract tests passed for the four
cognitive actions, the inference actions including their approved `run_id`
fields, `DescribeStructuredImage`, and `GenerateStructuredChat`. Each
cognitive goal also accepts an optional `prompt_name`; an empty name keeps the
frozen configured prompt, while a supplied name selects an existing prompt resource.
The checks also covered management actions, lifecycle services and status messages.
`ros2 interface show` resolved both `Model` and `GenerateStructuredChat` from
the isolated overlay. This installation is a temporary verification artifact,
not an installation into the normal workspace.

Real ROS clients exercised the four stage servers with generated action types.
A single-process test checked default and explicit capabilities, rejection of
unsupported capabilities, two script-controlled cycles, source-record links,
and an aborted request for an invalid run. A separate test started four stage
processes and ran `execute_experiment` across DDS. It confirmed stage order,
run identity, persistence links, the exact server-captured freeze, release,
completed lifecycle state, and clean process termination. Sensing, text generation,
planning output and robot execution were mocked. A further ROS test exercised
freeze acquisition and release against the inference server with an injected
weight-free backend: the server rejected standalone inference and a parameter
change during the freeze, but accepted a request owned by the active run.

These tests establish communication in the local DDS environment. They do not
exercise real inference, sensors, robot hardware, simulation, or GPU operations. The two
Python packages were installed into a clean copied-package overlay, and the
installed `smpa` command completed a run against separate mocked stage processes
and a weight-free inference server. The command was exercised outside the source
checkout with the installed Python packages on its import path. An operating-system interrupt during a held Sense request was
also checked: the run remained incomplete, was marked interrupted, and released
the inference freeze after the stage terminated. A mocked ROS camera publisher
exercised two installed-command cycles with actual image and description files;
Model records retained links to both evidence files. The server snapshot now also
records the loaded model device and dtype when its backend exposes them, preserving
the existing runtime metadata and frozen-parameters format. An interrupt during a real
inference backend call remains unverified. The conversation reply backend
remains unchanged. The legacy Runner and compatibility entry points remain in the
repository until their remaining regression coverage has been migrated. Some
legacy tests require different per-stage generation controls within one run;
the server source now supports separate frozen settings for each stage. The
inference action definitions were regenerated in the isolated overlay after the
host cooled, using one build worker without new throttle events. A full four-stage
ROS sequence now sends Model and Plan inference actions to one fake backend and
checks their distinct frozen controls and persistence links. The installed command
also passed completion, interruption, and camera-evidence tests after the Python
packages were refreshed. Legacy Runner regression coverage still requires migration
before the Runner can be removed without dropping assertions.

To reproduce the generated interface build on a stable authorized host:

```bash
cd /home/dorteel/ros2_ws/smpa-cognitive-nodes
source /opt/ros/jazzy/setup.bash
export CMAKE_BUILD_PARALLEL_LEVEL=1 MAKEFLAGS=-j1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
colcon --log-base /tmp/cognitive-stage/colcon-log build \
  --base-paths "$PWD/smpa_ros_interfaces" --packages-select smpa_ros_interfaces \
  --executor sequential --parallel-workers 1 --build-base /tmp/cognitive-stage/build \
  --install-base /tmp/cognitive-stage/install
```

To run the generated-interface, ROS transport, script-sequence and lifecycle
checks with local DDS discovery:

```bash
cd /home/dorteel/ros2_ws/smpa-cognitive-nodes
source /opt/ros/jazzy/setup.bash
source /home/dorteel/ros2_ws/install/setup.bash
source /tmp/cognitive-stage/install/local_setup.bash
export PYTHONPATH="$PWD:$PWD/smpa_ros:$PWD/smpa_llm:$PYTHONPATH"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
/home/dorteel/ros2_ws/src/smpa-ros/.venv/bin/python -m pytest -q \
  -c /dev/null -p no:cacheprovider test/test_cognitive_ros_transport.py \
  test/test_tier1_interfaces.py test/test_action_sequence.py \
  test/test_inference_freeze.py test/test_stage_wait.py test/test_smpa_control.py
```

## Per-stage inference configuration

Inference goals accept an optional `stage` selector. An empty selector keeps
the existing standalone or conversational generation settings. Cognitive nodes
send `sense`, `model`, or `plan`; focused visual observations reuse `sense`
settings. The server freezes the selected model's resolved settings for every
stage in `frozen_settings_json`. Each stage checks that snapshot against both
its run metadata and requested configuration before sending inference. Model
parameter changes are rejected while the run owns the server.

Each registered model has `models.<alias>.stages.<stage>` parameters for
`max_new_tokens`, `temperature`, `top_p`, `seed`, and `reasoning_effort`. The
first four default to inheritance from the model's base generation parameters
using sentinels `-1`, `-1.0`, `-1.0`, and `-2`, respectively. An empty
`reasoning_effort` means no reasoning control. Nonempty reasoning values are
forwarded only to API backends; the backend still validates provider support.
For example, these two namespaces can be set before an experiment acquires the
server:

```yaml
llm:
  ros__parameters:
    models:
      qwen3_vl_2b:
        stages:
          model:
            max_new_tokens: 512
            temperature: 0.2
          plan:
            max_new_tokens: 256
            temperature: 0.0
```

The values above illustrate distinct settings for the registered shared model.
Reasoning controls require an API model and provider that actually supports them. At startup the experiment submits its Sense, Model, and Plan settings together
with freeze acquisition in one atomic parameter transaction. Inherited stage
overrides are initialized from the experiment; an explicit ROS override must
match or the entire transaction fails. The resulting server snapshot is checked
and persisted before inference. A stage temperature must be explicit; null is
not representable by the current server controls and is rejected. A mismatch
fails rather than recording settings that were not used. Unit tests with
mocked backends cover isolation, inheritance, freezing, ownership and forwarding.
A local ROS parameter-service test also verifies atomic stage initialization and
conflict rejection with a fake backend.
Generated action contracts and end-to-end local ROS behavior for the new selector
have been verified with a fake backend. Real model and hardware behavior remain
outside these tests.
