# SMPA integration contracts (Sense workspace, iteration 1)

This document is the boundary between the GUI (owned by the GUI work in `smpa_gui/`, formerly `gui/`) and SMPA
(owned by Codex). It lists, separately:

1. **Interfaces SMPA already provides** and the GUI consumes today. Verified by reading
   SMPA's source and real run directories on 2026-11-15. Nothing in SMPA was changed.
2. **The gateway contract** the GUI exposes, which alternative frontends (for example
   TouchDesigner) can use without knowing SMPA's files.
3. **Interfaces requested from Codex**, with exact data shapes, for what cannot be done
   correctly today. The GUI does not implement substitutes for them.

The GUI never writes into SMPA's memory directory, imports no SMPA module, takes no file
locks, and offers no session creation, chat submission or observation writing.

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

## 2. Gateway contract for any frontend

The GUI server exposes the above as read-only HTTP JSON under `/api/v1/smpa/` (see
[API.md](API.md#smpa-sessions-read-only)): sessions with activity, normalized observations,
and recorded files. Live camera and Listen state use the existing sensor and echo
WebSocket streams. A TouchDesigner network can poll these URLs (Web Client DAT) and fetch
images (Movie File In TOP with a URL) without access to SMPA's disk or ROS.

The normalized observation record is the same shape requested in C2 below, so a native
SMPA index could replace the file adapter without changing any frontend.

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

### C4. Conversation input (decision needed, not implemented)

The Read panel is read-only by design of this iteration. If the SMPA tab should accept
text, Codex needs to decide where submission lives. The GUI needs only:

- one operation that submits user text to a named conversation session and returns the
  identity of the resulting recorded Sense record (or an error saying it was not recorded),
- a statement of whether submission requires the session to be live (C1),
- the same local-only access rules as the GUI's action routes.

The GUI will not add a submission route of its own.

### C5. Session lifecycle control (decision needed, not implemented)

Starting, stopping or creating sessions stays with `run_smpa.sh` and the conversation GUI.
A future control interface should say how a client starts a session with a configuration,
learns its run id immediately, and requests a clean stop that finalizes `metadata.yaml`.

### Not requested

- **Live Hear detail.** The Listen action already publishes feedback (`state`,
  `elapsed_seconds`). The GUI can show it when started with the SMPA workspace overlay
  (`SMPA_GUI_OVERLAY`); iteration 1 uses only the status topic, which needs no SMPA types.
- **Push events.** Polling the run directory every two seconds is sufficient for Sense; C2
  makes it cheap.

## 4. Functional, demonstration, absent

| Feature | State |
|---|---|
| Session list, lifecycle, modalities, Read binding | Functional, from §1.1 |
| Robot-run activity | Functional via ROS presence (§1.4); exact with C1 |
| Conversation-session activity | Shown as "running (unverified)" until C1 |
| See, Hear, Read recorded evidence and timeline | Functional, from §1.2 and §1.3 |
| Live camera, live Listen state | Functional, from §1.4 |
| Demonstration data | `test/fixtures/smpa_memory.py --demo` writes sessions in SMPA's formats plus the GUI marker `.smpa-gui-demo`; the GUI then shows a DEMONSTRATION DATA banner |
| Text input, session control | Absent by decision (C4, C5) |
