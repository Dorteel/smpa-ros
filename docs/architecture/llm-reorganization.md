# Inference package migration

Branch: `feature/llm-reorganization`, based on `89462f4`.

## Responsibilities and dependencies

The robot keeps its explicit Sense → Model → Plan → Act execution. Runner owns
one shared inference instance. Conversation keeps its independent Sense → Model
→ Act execution and existing Markdown/RDF persistence. No ROS contracts change.

| Owner | Responsibilities | Dependencies |
| --- | --- | --- |
| `smpa_ros` | Runner, sensory ROS adapters, cognition, planning schemas, actions, conversation, beliefs, provenance | `smpa_llm`, interfaces, ROS, external action servers |
| `smpa_llm` | Backend implementations, generation controls, model registry/provisioning, specialized inference, offline training | Inference libraries; training uses its separate environment |
| `smpa_ros_interfaces` | Existing services and actions only | ROS interface generators |
| `gui` | Existing independent browser presentation and ROS adapter | Unchanged by this migration |

## Sequence and rollback

Each phase is a separate commit. Roll back a phase by reverting its commit on
this feature branch; retain existing model weights and experiment data unchanged.
Do not reset a source branch or migrate historical records.

1. **Package boundary.** Move root cognitive metadata into `smpa_ros/`, and its
   Python implementation into `smpa_ros/smpa_ros/`. Keep `runner`, `memory`, `sense`
   and `speech` public imports and installed executable names. Update installer,
   colcon test paths, source-path assertions and installed-resource tests. Verify
   automatic discovery, three-package build, imports, resources and unit tests.
   No runtime performance effect is expected from metadata relocation.
2. **Generic inference.** Move `backends` implementations into
   `smpa_llm/smpa_llm/inference/backends/`. Keep stage generation and fixed action
   schema validation in cognition. Move conversational generation validation to
   inference; preserve old module identity for dependency injection. Verify
   strict schemas, all providers, roles, reasoning metadata and model sharing.
   Imports add no model load, process or serialization.
3. **Model management.** Move registry YAML and provisioning implementation to
   the shared package. Extract selection and asset verification from cognitive
   Model. Preserve lazy loading, revisions, weight paths and downloader commands.
   Verify manifests, model discovery, downloader failure recovery and installed
   registry lookup. Do not move or download existing large weights.
4. **Specialized inference.** Move speech engines and segmentation inference to
   the shared package, retaining ROS adapters, cancellation, service names and
   independent evaluator lifecycles. Verify speech shutdown, segmentation and
   VisualJEV, including injected devices. Do not insert evaluators into the loop.
5. **Training.** Move the gym implementation under shared training ownership.
   Preserve the launcher, separate environment, worker lifecycle, approval gates,
   immutable feedback, SFT/DPO and checkpoint formats. Run the real RLHF suite.
6. **Configuration and launch.** Give cognitive and inference parameters one
   owner each, resolve installed defaults with ament resources and preserve
   legacy overrides. Verify launch arguments, source and installed commands,
   model paths and independent gym execution. No inference node is introduced.
7. **Documentation and verification.** Update existing usage documentation,
   run unit/integration/RLHF suites and installed checks, inspect whitespace,
   record skips and actual latency/memory measurements. Confirm main and GUI
   remain unchanged. The next milestone is a shared inference ROS node and its
   services/actions; it is outside this migration.

## Baseline limitation

Before moving inference implementations, unit tests passed (328 passed, two
optional skips), and RLHF passed (12 tests). The all-senses integration test
exceeded its existing 360-second deadline twice during Model generation after
successful Sense persistence. The other three integration tests passed. The
second run was serial. This is an unresolved baseline failure, not a verified
migration regression. Logs are outside the repository in
`/tmp/smpa-llm-step1/`. No timeout or assertion was weakened.
