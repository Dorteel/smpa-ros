# SMPA integration contracts (Sense workspace and Dashboard)

This document is the boundary between the GUI (owned by the GUI work in `smpa_gui/`, formerly `gui/`) and SMPA
(owned by Codex). It lists, separately:

1. **Interfaces SMPA already provides** and the GUI consumes today. Verified by reading
   SMPA's source and real run directories on 2026-11-15. Nothing in SMPA was changed.
2. **The gateway contract** the GUI exposes, which alternative frontends (for example
   TouchDesigner) can use without knowing SMPA's files.
3. **Interfaces requested from Codex**, with exact data shapes, for what cannot be done
   correctly today. The GUI does not implement substitutes for them.

The GUI never writes into SMPA's memory directory itself, imports no SMPA module and takes
no file locks. Since version 4 the Dashboard starts SMPA's own conversation server
(§1.5) and relays conversation requests to it; that server, not the GUI, creates runs and
writes transcripts. The GUI offers no observation writing and writes no Turtle file.

## 1. Interfaces SMPA provides today

### 1.1 Run directories (`memory.py`, `MemoryManager`)

`<memory root>/runs/<started>_<run_name>_<run uuid>/`, one per session. The GUI reads:

| File | Fields used |
|---|---|
| `metadata.yaml` | `run_id`, `run_name`, `started_at` (ISO 8601 with offset), `completed_at`, `status` (`running`, `completed`, `interrupted`, `failed`), `completed_cycles`, `configuration.kind` (`conversation` marks a conversation session), `configuration.service` (`vision`, `speech`, `all`), `configuration.pipeline.camera_topic`, `configuration.pipeline.sensing.{hear, vision, read.enabled, read.session}` |
| `state/cycle_<n>/sense*.json` | `stage`, `cycle`, `sequence`, `invocation`, `started_at`, `prompt_name`, `previous_record`, `source_record`, `status` (`generated`, `failed`, `interrupted`), `error`, `output`, `source_observations`, `read_cursor` |

Enabled modalities are derived with the same rules as `sense.modalities()`: `service` gives
the defaults, `sensing.hear` and `sensing.vision` override them, Read requires
`sensing.read.enabled`.

### 1.2 Sense output inside a Sense record (`smpa_ros/sense.py`, `sense/read`)

`output` is a JSON string. Keys used by the GUI:

- `vision`, `speech`: text, also present as evidence files (below); not duplicated in the timeline.
- `read`: `{session_id, session_path, observed_at, observations: [{role, content, message_index,
  timestamp, epistemic_status, source, source_record, provenance}], cursor, deferred?}`.
- Scene refinement: `initial_observation`, `focused_observations[].source_record` (links a
  focused answer file to the Sense record that incorporated it).

### 1.3 Evidence files

| Modality | Files | Time stamp used |
|---|---|---|
| Vision | `observations/vision/<YYYY-MM-DD_HH-MM-SS_ffffff>Z_sense_output.md` (description) and `.png` (frame, saved before inference; may exist alone after a failure). PNG text chunk `smpa.camera` = `{topic, received_at, stamp, frame_id, encoding, width, height, step, is_bigendian}` | File name (UTC); `received_at` for frames without description |
| Vision (older runs) | Same names without `Z` (local time), in `observations/vision/` or `vision/` | File name, with the run's start offset applied |
| Vision (refinement) | `observations/vision/frozen_image.png`, `observations/vision/observe_<cycle>_<n>.json` (`target`, `question`, `output`, `image_sha256`, `model_identifier`) | File modification time (no time is recorded) |
| Hear | `observations/audio/<YYYY-MM-DDTHHMMSS_ffffff>Z_<random>/recording.wav` (16 kHz mono float32) and `transcript.md` (`- Timestamp:`, `- Run ID:`, `- Source:`, `- Language:`, `- Recognition model:`, `- Status:`, `## Transcript`) | `Timestamp` |
| Read (conversation session) | `state/cycle_<n>/sense.json` with `output` = the user message (`smpa_ros/conversation.py`) | `started_at` |

The pre-run archive `memory/legacy/` (flat vision files and `audio/`) is shown as one
read-only archive session.

### 1.4 ROS

- A running robot run owns ROS names under `/run_<uuid with underscores>` (`runner.py`).
  Their presence on the ROS graph is the GUI's liveness check for robot runs.
- With Hear enabled, its Listen action's standard status topic
  `/run_<id>/sense/hear/listen/_action/status` (`action_msgs/msg/GoalStatusArray`) tells
  whether SMPA is listening right now (a goal with status 1 or 2).
- The run's `camera_topic` provides the live picture. It is labelled live: it is not what
  SMPA recorded.

### 1.5 Conversation server (`smpa_ros/smpa_ros/conversation_web.py`), used by the Dashboard

Verified against SMPA's source on `main` 717094b (after the ROS 2 package restructuring,
`feature/llm-reorganization`) and by running it in the cloud with a test configuration and
with SMPA's own configuration on 2026-11-18. The earlier flat layout
(`smpa_ros/conversation_web.py` at the repository root) behaves the same and is still
recognised.

- Started as `python -m smpa_ros.conversation_web --config <config.yaml> --world-model-root
  <memory> --model-root <models> --port <port> [--resume <run directory>]` with SMPA's
  `.venv`, working directory = the checkout, and `PYTHONPATH` = `<root>/smpa_ros:<root>/smpa_llm`
  (the GUI sets this; an installed workspace works too). Without `--resume` it **creates a
  conversation run** at start. SIGINT/SIGTERM finalize the run.
- Configuration: `smpa_ros/config.yaml` holds the cognitive settings and selects the
  inference defaults with `inference_config: package://smpa_llm/config.yaml`
  (`smpa_llm.configuration.load_configuration`: top-level entries of the cognitive file
  replace whole sections of the defaults). `package://smpa_llm` and the model registry
  (`models/model_library.yaml` beside the configuration, else `share/smpa_llm/models`)
  are found only through the sourced ROS workspace. Model weights: `<cwd>/models` for
  such a configuration, `models/` beside a complete legacy configuration. API keys:
  environment, else `.env` beside the configuration, else `.env` in the working directory.
- One run per process, served on `127.0.0.1`; the transcript is locked with `flock`.
  Single-threaded: one request at a time.
- `GET /api/conversation` (also `/api/agents`, `/api/graph`): `messages`, `run_id`,
  `active_agent`, `agents` (`source`, `id`, `label`, `available`, `availability`, …),
  `generation`, `modelling_error`, `runs`.
- `POST /api/message {message}`, `/api/agent {agent_id}`, `/api/new {}`,
  `/api/resume {run_id}` (same pipeline configuration required), `/api/settings`,
  `/api/clear`. Errors: 400 `{"error"}`; inference failure 500 with the state and "Your
  message is saved"; a foreign `Origin` 403 (the GUI sends none).
- Agents: `local:<name>` for registry entries with backend `transformers`/`ollama`,
  `api:<provider>` for providers with `models.text`, `base_url`, `api_key_env`; the
  start-up agent is `conversation.agent`, belief modelling uses `conversation.modelling_agent`.
- Transcript `<run>/conversation.md`: `# Conversation` header, then entries
  `<!-- smpa-message <Role> <bytes> [agent <bytes>] [parts <thinking> <response>] -->`,
  optional `<!-- smpa-agent {json} -->`, `<a id="message-<n>"></a>`, `## <Role>`, body,
  blank line. Belief modelling writes `<run>/belief-state.ttl` and `conversation-sds.md`.

### 1.6 Per-run files read by the Dashboard

Configurable lists of candidates relative to a run directory (`files.*` in the GUI
layout); the first existing candidate is used and the Dashboard says when it is a fallback:

| View | Target location (preferred) | Where SMPA writes today (fallback) |
|---|---|---|
| Conversation | `world_model/conversation.md` | `conversation.md` |
| Sense | `world_model/sense.ttl` | — (not produced) |
| Belief | `world_model/belief.ttl` | `belief-state.ttl` (conversation runs; starts as a comment-only file) |
| Plan | `world_model/plan.ttl` | — (not produced) |

Any valid Turtle is drawn; no vocabulary is assumed. The GUI never writes these files.

## 2. Gateway contract for any frontend

The GUI server exposes the above as read-only HTTP JSON under `/api/v1/smpa/` (see
[API.md](API.md#smpa-sessions-read-only)): sessions with activity, normalized observations,
and recorded files. Live camera and Listen state use the existing sensor and echo
WebSocket streams. A TouchDesigner network can poll these URLs (Web Client DAT) and fetch
images (Movie File In TOP with a URL) without access to SMPA's disk or ROS.

The normalized observation record is the same shape requested in C2 below, so a native
SMPA index could replace the file adapter without changing any frontend.

The Dashboard's routes (`/api/v1/smpa/models`, `/conversation…`, `/graphs/…`) are the same
kind of adapter: a frontend can hold a conversation and draw the graphs without knowing
SMPA's files, process or server protocol.

## 3. Interfaces requested from Codex

Each item states the gap, why it matters, and the exact contract the GUI would consume.
Field names and semantics are proposals for Codex to accept, change or reject; the GUI
will adapt to whatever Codex decides.

### C1. Session liveness heartbeat (needed)

**Gap.** `status: running` stays forever when SMPA is killed. For robot runs the GUI uses
ROS presence as a substitute, which fails across ROS domains or machines. Conversation
sessions have no ROS presence, so their liveness cannot be determined at all.

**Contract.** While a session's process runs, SMPA updates a `process` block in
`metadata.yaml` (with its existing atomic `write_metadata`) at least every 5 seconds:

```yaml
process:
  pid: 12345
  hostname: dorteel-novacustom
  started_at: '2026-11-15T10:00:00.000000+01:00'   # process start
  heartbeat_at: '2026-11-15T10:03:25.120000+01:00' # last update, ISO 8601 with offset
  heartbeat_interval_s: 5
```

GUI rule: active if `status == running` and `now - heartbeat_at < 3 × heartbeat_interval_s`;
otherwise "running, no heartbeat". Applies to robot and conversation sessions alike.

### C2. Append-only observation index (recommended)

**Gap.** The GUI infers observations from file names and three layouts, with time stamps
from file names, transcripts or modification times. Focused observations and frozen images
have no recorded time.

**Contract.** SMPA appends one JSON object per line to `observations/index.jsonl` when it
has durably written a piece of evidence (write the line with a single `write` + `fsync`;
never rewrite earlier lines):

```json
{"schema": "smpa.observation/1", "observation_id": "uuid", "session_id": "run uuid",
 "modality": "vision|hear|read", "kind": "scene_description|camera_frame|focused_observation|audio_transcript|user_message",
 "observed_at": "2026-11-15T09:00:05.250000+00:00", "cycle": 1, "sense_record": "state/cycle_000001/sense.json",
 "status": "generated|failed|evidence_without_description|...", "text": "…optional…",
 "files": {"image": "observations/vision/….png", "audio": null, "text": "observations/vision/….md"},
 "epistemic_status": "unverified_user_statement|null", "provenance": {"…": "free-form, as recorded today"}}
```

With this index the GUI reads one file per session and no longer depends on file-name
conventions. Existing files stay as they are; the index only adds a manifest.

### C3. Recorded times for refinement evidence (small)

Add `observed_at` (ISO 8601 with offset) to `observe_<cycle>_<n>.json`, and a
`frozen_at` with the image's `smpa.camera` text chunk to `frozen_image.png`, as normal
camera PNGs already have. Until then the GUI shows file modification times and labels them so.

### C4. Conversation input (resolved for conversations)

Conversation input now goes through SMPA's existing conversation server (§1.5), started
by the GUI. Robot runs still have no text input of their own.

### C5. Session lifecycle control (partly resolved)

Conversation runs are started, reopened and stopped through SMPA's conversation server.
Robot runs remain with `run_smpa.sh`; the request for a control interface there stands.

### C6. Transcript location `world_model/conversation.md` (requested)

The requested structure persists each conversation in `world_model/conversation.md`
within its run. SMPA writes `<run>/conversation.md` (unchanged by the restructuring).
Moving it belongs to SMPA (its reader, writer, lock, resume and the belief provenance
links `conversation.md#message-n` all use that path). The GUI already prefers
`world_model/conversation.md` and falls back to `conversation.md`, so a move needs no GUI
change. Please keep the format of §1.5 or announce a new one, and do not write two copies.

### C7. Producer contract for `sense.ttl`, `belief.ttl`, `plan.ttl` (requested)

- Location per run: `world_model/{sense,belief,plan}.ttl` (the GUI's preferred
  candidates). Today the belief graph is `<run>/belief-state.ttl`, which the GUI shows as
  a fallback; please reconcile the names (one file, not a copy).
- Replace files atomically (write a temporary file in the same folder, `fsync`, rename),
  so readers never see half a graph. The GUI tolerates partial files but must then show
  the previous version.
- UTF-8 Turtle; an empty graph is a file without triples (not a missing file); a
  missing file means "not produced (yet)".
- Optional but useful: a `# revision:` comment or a sidecar with the producing cycle,
  so a viewer can say which cycle a graph belongs to. Historical snapshots stay out of
  scope for the GUI.

### C8. Model descriptions in the registry (small)

`model_library.yaml` and the provider blocks have no description. An optional
`description:` per entry would be shown first by the GUI (already supported); without
it the GUI shows configuration facts.

### C9. Conversation server conveniences (optional)

- List agents without creating a run (for example `--list-agents` printing JSON), so
  availability can be verified before a conversation starts.
- Accept the initial agent at start (`--agent <source>`), so a run does not start with
  the configured default and switch immediately (both are recorded today).
- Include the run directory in the state, and answer status requests while a reply is
  being generated (a threaded server, or a lightweight `/api/status`).
- Stream or report generation progress (for example tokens so far); today the GUI can
  only say how long a reply has been running.

### C10. Stable start interface for the conversation server (needed for packaging)

The GUI starts the server as a Python module from the checkout and must know the
package layout (`smpa_ros/smpa_ros`, `smpa_llm`) to set `PYTHONPATH`. A stable interface
would let it use an installed SMPA without knowing the source layout, for example:

- `ros2 run smpa_ros conversation --config <file> --world-model-root <dir> --port <port>`
  documented as the supported way (the console script exists; its arguments and the
  run-creating behaviour should be declared stable), and
- the installed path of the default configuration (`share/smpa_ros/config.yaml`) and of
  the model root declared, so a GUI launched from an install space needs only the
  workspace, not the checkout.

### C11. Interfaces Codex must eventually provide or stabilize (summary)

1. Conversation server start/stop interface and its HTTP API (§1.5) as a stable contract (C10).
2. Transcript location and format, ideally `world_model/conversation.md` (C6).
3. Producer contract for `world_model/{sense,belief,plan}.ttl`: location, atomic
   replacement, empty versus missing (C7).
4. Session liveness heartbeat for conversation and robot runs (C1).
5. Listing agents without creating a run, initial agent at start, run directory and
   progress in the state (C9).
6. Optional model descriptions in the registry (C8); observation index (C2); recorded
   times for refinement evidence (C3); robot-run lifecycle control (C5).

### Not requested

- **Live Hear detail.** The Listen action already publishes feedback (`state`,
  `elapsed_seconds`). The GUI can show it when started with the SMPA workspace overlay
  (`SMPA_GUI_OVERLAY`); iteration 1 uses only the status topic, which needs no SMPA types.
- **Push events.** Polling the run directory every two seconds is sufficient for Sense; C2
  makes it cheap. The graph views poll every 1.5 s and redraw only on a changed content
  digest, so no file-change notifications are needed either.
- **Graph history.** The GUI shows the current file only; snapshots and reconstruction
  of earlier graph states are out of scope.

## 4. Functional, demonstration, absent

| Feature | State |
|---|---|
| Session list, lifecycle, modalities, Read binding | Functional, from §1.1 |
| Robot-run activity | Functional via ROS presence (§1.4); exact with C1 |
| Conversation-session activity | Shown as "running (unverified)" until C1 |
| See, Hear, Read recorded evidence and timeline | Functional, from §1.2 and §1.3 |
| Live camera, live Listen state | Functional, from §1.4 |
| Demonstration data | `test/fixtures/smpa_memory.py --demo` writes sessions in SMPA's formats plus the GUI marker `.smpa-gui-demo`; the GUI then shows a DEMONSTRATION DATA banner |
| Conversation (models, start, chat, reopen, continue) | Functional, through SMPA's conversation server (§1.5) |
| Sense, Belief, Plan graph views | Functional for any Turtle file at the configured paths; Belief falls back to SMPA's `belief-state.ttl`; SMPA does not produce `world_model/*.ttl` yet (C7) |
| Mock graphs | `smpa_gui/smpa_gui/mock_graphs/`, reserved namespace, MOCK DATA banner; never written into runs |
| Robot-run text input, robot-run lifecycle control | Absent (C5) |
