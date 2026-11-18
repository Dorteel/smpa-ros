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
