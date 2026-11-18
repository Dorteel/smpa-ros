# Backend interface inventory for GUI integration

Base: merged `main` at `0479bc2`. This inventory describes source contracts;
an older workspace installation can still lack the cognitive actions and nodes.
The historical inventory in `cognitive-node-inventory.md` is not a description
of the current execution path. No `gui/` files or ROS wire definitions change
in this backend work.

## Status and ownership

| Item | At the base commit | This branch |
|---|---|---|
| C1 session liveness | Missing; `running` alone and ROS graph presence cannot prove liveness | Owner heartbeat in atomic `metadata.yaml`, for the experiment entry point and conversation HTTP server |
| C2 observation manifest | Missing; evidence and provenance exist, but require several file readers | Append-only `observations/index.jsonl` at existing evidence writers |
| C3 refinement times | Missing from focused JSON and frozen PNG | `observed_at` in new focused JSON; `frozen_at` and camera provenance inside new frozen PNGs |
| C4 conversation submission | Existing loopback conversation HTTP API; no operation targeting an arbitrary session and returning its Sense record | Reuse existing API; remaining addressing/acknowledgement decision below |
| C5 session lifecycle | Robot start/stop/status services and conversation create/resume APIs already exist | Reuse them; no unified robot/conversation controller added |

`smpa.py` still chooses stage order, repetition and experiment boundaries.
Sense/Model/Plan/Act execute their own operations, `smpa_llm` owns inference,
and `MemoryManager` remains shared Python persistence. Heartbeats are started
only by session owners, never by attached stage workers or merely reading a run.

## Existing ROS inventory: implemented, unchanged

All types below belong to `smpa_ros_interfaces`, with their existing CMake
registration. Names are defaults; ROS namespaces/remappings still apply.
There are 14 actions, eight services and two notification messages, including
all original 13 Tier 1 types.

| Owner | Default names | Types |
|---|---|---|
| `smpa_ros` stage nodes | `/smpa/sense`, `/smpa/model`, `/smpa/plan`, `/smpa/act` | `Sense`, `Model`, `Plan`, `Act` actions |
| `smpa_llm` | `/llm/generate`, `/llm/generate_structured`, `/llm/generate_structured_chat` | `GenerateText`, `GenerateStructured`, `GenerateStructuredChat` actions |
| `smpa_llm` | `/llm/describe_image`, `/llm/describe_structured_image` | `DescribeImage`, `DescribeStructuredImage` actions |
| `smpa_llm` | `/llm/load_model`, `/llm/unload_model`, `/llm/set_active_model` | `LoadModel`, `UnloadModel`, `SetActiveModel` actions |
| `smpa_llm` | `/llm/list_models`, `/llm/get_model_status` | `ListModels`, `GetModelStatus` services |
| `smpa_llm` | `/llm/inference_status` | `InferenceStatus` topic |
| `smpa_ros` control | `/smpa/start`, `/smpa/stop`, `/smpa/get_status` | `StartSmpa`, `StopSmpa`, `GetSmpaStatus` services |
| `smpa_ros` control | `/smpa/events` | `SmpaEvent` topic |
| Existing sensory/robot adapters | `/sense`, `/segment_image`, `/visual_jev/evaluate` | `DescribeScene`, `SegmentImage`, `EvaluateVisualDecision` services |
| Existing speech adapters | `/speech/listen`, `/speech/talk` (run launch remaps these) | `Listen`, `Talk` actions |

The four stage executables are `sense_stage`, `model_stage`, `plan_stage` and
`act_stage`; inference is `smpa_llm/llm_server`. The stage goals already carry
`run_id`, optional `capability` and `prompt_name`, plus their request-specific
payloads. Model also supports belief updates. Inference goals already carry
`run_id` and `stage`. Per-model and frozen per-stage generation parameters,
ownership checks, schema validation and cancellation limitations are unchanged.

## C1: owner heartbeat

The existing atomic metadata writer merges each refresh with the current run
under its cooperative lock. It preserves stage writes and does not mutate the
experiment owner's in-memory dictionary from a background thread.

```yaml
process:
  pid: 12345
  hostname: laptop
  started_at: '2026-11-18T12:00:00+00:00'
  heartbeat_at: '2026-11-18T12:03:25+00:00'
  heartbeat_interval_s: 5
```

`started_at` identifies the actual Linux process start, using `/proc`, not a
resumed session's original start. Refreshes continue during blocked inference
or idle HTTP service. Finalization stops them; closing the conversation server
also stops them without inventing a terminal state. Successful conversation
switches transfer liveness to the selected session. Failed writes are logged
and retried. A run lock or stalled owner may delay a refresh; stale liveness is
reported honestly rather than synthesized by another process.

Consumer rule: active only when `status == running` and heartbeat age is less
than three intervals. A killed process leaves its last timestamp and `running`
status; after 15 seconds it is **running, no heartbeat**, not a fabricated
completed/failed session. This rule works without ROS discovery or a local PID
lookup. A library caller creating a MemoryManager is not automatically a live
server: explicit process owners call `start_heartbeat()` and stop/finalize it.

## C2: append-only evidence manifest

Each UTF-8 JSON line has the requested `smpa.observation/1` schema:

```json
{"schema":"smpa.observation/1","observation_id":"uuid","session_id":"run uuid","modality":"vision","kind":"scene_description","observed_at":"2026-11-18T12:00:05+00:00","cycle":1,"sense_record":"state/cycle_000001/sense.json","status":"generated","text":"A cup.","files":{"image":"observations/vision/example.png","audio":null,"text":"observations/vision/example.md"},"epistemic_status":null,"provenance":{"topic":"/camera","received_at":"2026-11-18T12:00:05+00:00"}}
```

| Producer | Kind | Timing and provenance |
|---|---|---|
| Camera capture | `camera_frame` | Indexed before inference; original ROS header, receipt time and original image path. Initial status `evidence_without_description` remains historical even when a later description succeeds |
| Camera description | `scene_description` | Separate successful entry, original frame time, image and description paths |
| Refinement freeze | `camera_frame` | One entry for the exclusively published frozen PNG, including its freeze and source times |
| Act focus | `focused_observation` | Focus-operation start time, question/target, image hash, model, camera provenance and originating `act_record` |
| Listen persistence | `audio_transcript` | Complete or incomplete terminal transcript, capture timestamp, audio/text paths, language/model/error |
| Conversation Sense | `user_message` | Saved User text, exact conversation Sense record and message index; `unverified_user_statement` |
| Robot Sense Read | `user_message` | Copied user statement with original timestamp, source session/record/message provenance and the robot's planned Sense record |

Paths in `files` are run-relative and must exist within the run. No source
conversation files are copied or modified by Read. Read entries contain inline
text and original external references in `provenance`; the local cursor commits
only after index writes succeed. If commit subsequently fails, an observed
message may be indexed without being consumed; a later Sense may observe it
again. The index is evidence, not an alternative authoritative Read cursor.

`cycle` and `sense_record` are nullable when unknown: standalone camera/Listen
requests have no experiment invocation in their existing ROS contract. Focused
evidence belongs to Act and may not yet have a consuming Sense; its origin is
in `provenance.act_record`. Do not guess a cycle from mutable metadata or assign
a future Sense. Existing Sense `source_observations` still provides incorporation
links. Model/Plan/Act are not automatically invoked by an index write.

One index-specific advisory lock serializes appends only; it does not hold the
shared run lock during inference or sensor operations. Each entry uses one
`os.write` plus `fsync`, with evidence directory entries synced first. Previous
lines are never rewritten. A storage error propagates to the producer and keeps
the evidence. An incomplete final line causes later appends to fail explicitly;
no automatic truncation or repair is performed. File and index publication are
not a multi-file transaction: a process killed between them can leave unindexed
evidence, including provisional audio. Consumers still need legacy evidence
fallback for old runs and those crash windows. No historical backfill occurs.

## C3: explicit refinement times

New `observe_<cycle>_<invocation>.json` files have `observed_at`, the UTC time
the focused operation began. This is not a new camera acquisition time.
New `frozen_image.png` files contain the existing `smpa.camera` PNG text chunk
with `topic`, `received_at`, ROS `stamp`, frame/image fields and `frozen_at`.
Normal stage capture preserves its actual receipt time; direct legacy service
requests record receipt at that service. Old frozen images are not rewritten,
and old evidence without these fields still needs the GUI's labelled fallback.

## C4/C5: existing APIs and exact outstanding decisions

The existing conversation server binds loopback. `POST /api/message` accepts
`{"message":"..."}` for its **active** session; its state response includes
`run_id` and messages, and its existing error response says when a user message
was saved but inference failed. It preserves the current reply backend. JSON
size limits and cross-origin rejection already exist. `GET /api/conversation`
returns current state; `POST /api/new`, `/api/clear` and `/api/resume` implement
the server's current conversation switching behavior. Resume accepts a known
`run_id` and requires the original configuration and transcript lease.

These endpoints do not provide a submission to any arbitrary named session
with a recorded Sense identity returned separately from reply inference. A
decision is still needed about whether clients must address only the currently
live server session or may resume/create sessions, and how to acknowledge a
saved User record independently of a failed reply. No new C4 route was added.

For robot experiments, `/smpa/start` already takes a `goal` and returns
`accepted` and `run_id` before inference setup; `/smpa/stop` stops its owned
child, and `/smpa/get_status` plus `/smpa/events` expose lifecycle transitions.
Configuration/memory/model roots are control-node parameters (the compatibility
parameter `runner_arguments` is only a name). Stage servers and inference remain
independent participants. These services do not create conversation servers,
and conversation HTTP APIs do not control robot runs. The remaining C5 decision
is a client contract for per-request configuration and targeting/stopping
conversation owners; do not infer that a shared cross-kind session owner exists.

## Documentation and verification boundary

`gui/SMPA_CONTRACTS.md` describes its inspected 2026-11-15 baseline. Its Runner
references and C1–C3 “missing” statements are outdated after this branch; its
ownership boundary and C4/C5 decisions remain applicable. Claude owns updating
that file and adapting the GUI. ROS graph presence is not the new liveness rule.

Focused checks passed **238 distinct cases** across the commands below:
158 affected regressions, 66 liveness/locking/stage/ROS-contract cases,
14 final boundary cases (12 repeat earlier coverage), and 12 installed-path
ROS/package cases. Two existing opt-in tests were skipped: browser interaction
and actual Qwen reply reproduction. The earlier Read assertion was strengthened
to distinguish legitimate owner appends from forbidden Read writes. No behavior
assertions were removed.

The four ROS cases exercise the installed experiment with four separate stage
processes and one weight-free inference server: completion, interruption/freeze
release, two camera-evidence cycles, and standalone rejection during a freeze.
The camera test checks the new index, exact run/cycle/Sense links and heartbeat.
The remaining eight cases check installed package resources and boundaries.
Installed imports were checked outside the source checkout. Python-only
installation took 2.12 seconds; a final changed-package refresh took 1.12 seconds.
Existing setuptools `tests_require` and disabled-bytecode warnings remain.

These checks reuse already generated, unchanged ROS definitions; no C++ or
model build was run. The normal workspace installation was not replaced.
Full clean builds, real inference, robot hardware, GPU, training and simulation
remain unverified. Detailed local logs and JUnit results are in
`/tmp/smpa-backend-interface-tests/`.

Exact local reproduction (requires the existing generated interface overlay):

```bash
cd /home/dorteel/ros2_ws/smpa-backend-interfaces
unset AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH TOKENIZERS_PARALLELISM
source /opt/ros/jazzy/setup.bash
source /tmp/cognitive-stage/install/setup.bash
export PYTHONPATH="$PWD:$PWD/smpa_ros:$PWD/smpa_llm:$PYTHONPATH"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1
export ROS_DOMAIN_ID=87 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export ROS_LOG_DIR=/tmp/smpa-backend-interface-tests/ros
test_python=/home/dorteel/ros2_ws/src/smpa-ros/.venv/bin/python
"$test_python" -m pytest -q --rootdir="$PWD" -c smpa_ros/setup.cfg -p no:cacheprovider \
  test/test_read.py test/test_speech_audio.py test/test_conversation.py \
  test/test_action_sequence.py test/test_run_storage.py test/test_act_logic.py test/test_loop.py
"$test_python" -m pytest -q --rootdir="$PWD" -c smpa_ros/setup.cfg -p no:cacheprovider \
  test/test_session_interfaces.py test/test_shared_run_lock.py test/test_sense_logic.py \
  test/test_tier1_interfaces.py test/test_smpa_control.py
"$test_python" -m pytest -q --rootdir="$PWD" -c smpa_ros/setup.cfg -p no:cacheprovider \
  test/test_session_interfaces.py \
  test/test_conversation.py::ConversationTests::test_generation_controls_and_run_lifecycle
```

Install only the Python packages and run the installed transport checks. The
transport fixture leases a separate DDS domain and skips the configured domain;
set that variable to the real user domain before running, not an arbitrary
test domain (42 is this laptop's current user domain).

```bash
unset AMENT_PREFIX_PATH CMAKE_PREFIX_PATH COLCON_PREFIX_PATH PYTHONPATH LD_LIBRARY_PATH TOKENIZERS_PARALLELISM
source /opt/ros/jazzy/setup.bash
source /tmp/cognitive-stage/install/setup.bash
export CMAKE_BUILD_PARALLEL_LEVEL=1 MAKEFLAGS=-j1
"$test_python" /usr/bin/colcon --log-base /tmp/smpa-backend-interface-tests/colcon-log build \
  --paths "$PWD/smpa_llm" "$PWD/smpa_ros" --packages-select smpa_llm smpa_ros \
  --executor sequential --parallel-workers 1 \
  --build-base /tmp/smpa-backend-interface-tests/build \
  --install-base /tmp/smpa-backend-interface-tests/install
source /tmp/smpa-backend-interface-tests/install/local_setup.bash
export ROS_DOMAIN_ID=42 ROS_LOG_DIR=/tmp/smpa-backend-interface-tests/transport-ros
"$test_python" -m pytest -q --rootdir="$PWD" -c smpa_ros/setup.cfg -p no:cacheprovider \
  test/test_cognitive_ros_transport.py::test_installed_smpa_command_runs_complete_experiment \
  test/test_cognitive_ros_transport.py::test_installed_smpa_command_interrupts_and_releases_freeze \
  test/test_cognitive_ros_transport.py::test_installed_experiment_preserves_camera_evidence \
  test/test_cognitive_ros_transport.py::test_real_inference_freeze_and_standalone_rejection \
  test/test_package_boundary.py
```
