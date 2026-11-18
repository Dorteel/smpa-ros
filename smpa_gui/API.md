# Gateway API, contract version 1.6

Base URL: `http://127.0.0.1:8765/api/v1`. All routes are read-only (HTTP `GET` or
WebSocket) except the `POST` routes for sending and cancelling action goals
([Action execution](#action-execution)), [setting parameters](#parameter-management),
[calling services](#service-management) and the [SMPA conversation](#smpa-conversation)
routes, which relay to SMPA's own conversation server. All of them follow the same
[access rules](#access-rules-for-the-post-routes).
Version 1.1 added the `/actions/...` routes, 1.2 the read-only `/smpa/...` session
routes, 1.3 (GUI v4 patch) the conversation, layout, model and graph routes, and 1.4
(GUI v5) the parameter-change and service routes, parameter descriptors, `/session` and
`/smpa/status.enabled`. Version 1.5 (GUI v6) combines 1.3 and 1.4 and adds `candidates`
and `fallback` to transcript and graph answers. Version 1.6 (GUI v7) adds the read-only
parameter overview `GET /ros/parameters` ([Parameter management](#parameter-management))
for the ROS 2 Workspace. Nothing earlier changed. Every JSON response contains `contract_version`. ROS names are passed as
URL-encoded query parameters because they contain slashes. A machine-readable
description of the HTTP routes is served at `/api/openapi.json`.

Errors use HTTP status codes with a JSON body `{"detail": "..."}`:
`404` unknown topic, `415` data that cannot be drawn, `503` ROS gateway unavailable,
`504` no data in time.

## Snapshots

### `GET /health`

```json
{"contract_version": "1.4", "status": "ok",
 "ros": {"ok": true, "node": "/smpa_gui", "domain_id": 0, "rmw": "rmw_fastrtps_cpp", "distro": "jazzy"},
 "active_subscriptions": [{"topic": "/scan", "type": "sensor_msgs/msg/LaserScan", "viewers": 1}]}
```

`active_subscriptions` lists what the gateway is subscribed to at this moment.

### `GET /ros/graph`

```json
{"contract_version": "1.4", "stamp": 1794679890.5,
 "nodes":   [{"id": "node:/talker", "name": "talker", "namespace": "/", "full_name": "/talker",
              "hidden": false, "is_gateway": false}],
 "topics":  [{"id": "topic:/chatter", "name": "/chatter", "types": ["std_msgs/msg/String"],
              "hidden": false, "infrastructure": false, "action": null,
              "publisher_count": 1, "subscriber_count": 1}],
 "edges":   [{"id": "publishes:/talker:/chatter:0", "source": "node:/talker", "target": "topic:/chatter",
              "kind": "publishes", "type": "std_msgs/msg/String", "qos": {}}],
 "actions": [{"id": "action:/fibonacci", "name": "/fibonacci", "types": ["example_interfaces/action/Fibonacci"],
              "servers": ["/server_node"], "clients": ["/client_node"]}],
 "services": [{"name": "/talker/get_parameters", "types": ["rcl_interfaces/srv/GetParameters"],
               "hidden": false, "action": null}]}
```

- An edge of kind `publishes` goes from a node to a topic; `subscribes` goes from a topic to a node.
- `hidden`: a name part starts with `_`. `infrastructure`: `/rosout` or `/parameter_events`.
- `action`: the action a hidden action topic or service belongs to, else `null`.
- `is_gateway`: the GUI's own node.
- `qos`: `{"reliability", "durability", "history", "depth", "liveliness", "deadline_s", "lifespan_s"}`.
  `history` is `unknown` and `depth` is `0` for endpoints of other processes, because ROS
  discovery does not transmit them. `null` durations mean unlimited.
- Action servers and clients are the publishers and subscribers of the action's status topic.

The result is cached for one second.

### `GET /ros/topics`, `GET /ros/nodes`, `GET /ros/actions`

`{"topics": [...]}`, `{"nodes": [...]}`, `{"actions": [...]}` with the same entries as in the graph.

### `GET /ros/topics/info?name=<topic>`

`{"topic": {<topic entry>, "publishers": [{"node", "type", "qos"}], "subscribers": [...]}}`.
`404` when the topic is not currently known.

### `GET /ros/nodes/parameters?node=<full node name>&timeout=<seconds, 0.2–10, default 2>`

```json
{"contract_version": "1.4", "node": "/talker", "state": "ok",
 "parameters": [{"name": "level", "type": "integer", "value": 4,
                 "description": "Even level from 0 to 10", "read_only": false, "dynamic_typing": false,
                 "additional_constraints": "", "declared_type": "integer",
                 "integer_range": {"from": 0, "to": 10, "step": 2}, "floating_point_range": null,
                 "descriptor_known": true}]}
```

The descriptor fields come from the node's `describe_parameters` answer. When the node
gave no descriptor, `descriptor_known` is `false` and `read_only`/`dynamic_typing` are
`null`.

`state` is `ok`, `not_responding` (the node did not answer in time; it may be busy or
not spinning) or `unavailable` (the node offers no parameter services or does not
exist). In the last two cases `parameters` is empty and the HTTP status is still `200`.
`type` is one of `not_set`, `bool`, `integer`, `double`, `string`, `byte_array`,
`bool_array`, `integer_array`, `double_array`, `string_array`.

### `GET /sensors/registry`

`{"sensors": [{"type": "sensor_msgs/msg/Imu", "viewer": "imu", "label": "IMU", "default_rate": 20}]}`

### `GET /sensors/topics`

`{"topics": [{"topic": "/scan", "type": "sensor_msgs/msg/LaserScan", "viewer": "laserscan",
"label": "Laser scan", "publisher_count": 1}]}`. Hidden topics are omitted.

### `GET /sensors/image/frame.jpg?topic=<topic>&type=&timeout=3&max_width=960`

One current frame as `image/jpeg`. Subscribes, waits for a frame, unsubscribes.

### `GET /sensors/image/stream.mjpg?topic=<topic>&type=&rate=&max_width=960`

`multipart/x-mixed-replace; boundary=frame` motion-JPEG for non-browser clients. The
subscription lasts as long as the connection.

## Streams (WebSocket)

Opening a stream subscribes the gateway to the topic; closing it unsubscribes. The
client never needs to send anything. Browser pages from other origins are refused.

| Stream | Parameters |
|---|---|
| `WS /ros/topics/echo` | `name` (topic), optional `type`, optional `rate` (default 5, max 20 per second) |
| `WS /sensors/stream` | `topic`, optional `type`, optional `rate` (default per sensor, max 30), `max_width` (images, 64–1920, default 960), `max_points` (point clouds, 100–100000, default 20000) |

The gateway sends two kinds of JSON text messages.

**Status** explains why no data is shown:

```json
{"kind": "status", "state": "no_publishers", "topic": "/scan", "detail": "No publisher on this topic"}
```

| `state` | Meaning |
|---|---|
| `waiting` | Publishers exist but no message arrived for two seconds |
| `no_publishers` | Nobody publishes the topic (the stream stays open and recovers by itself) |
| `unsupported` | Messages arrive but cannot be drawn (for example an unknown image encoding) |
| `error` | The stream cannot work (unknown topic or type, type not installed, no sensor viewer); the gateway closes it |

**Data** carries one message. Common fields: `kind: "data"`, `topic`, `type`, `rate_hz`
(messages per second received by the gateway, before rate limiting). If the header
contains `binary`, exactly one binary WebSocket message follows it.

### Echo data

`{"kind": "data", "message": {...}, "truncated": false, ...}`. `message` is the ROS
message as JSON. Sequences longer than 64 elements become
`{"__truncated__": true, "length": N, "head": [first 64]}`, byte strings become
`{"__bytes__": N}`, strings are cut at 2000 characters, and NaN or infinity become the
strings `"nan"`, `"inf"`, `"-inf"`. `truncated` says whether anything was shortened.

### Sensor data

All stamped sensor headers contain `frame_id` and `stamp` (seconds). Non-finite numbers
are sent as `null`.

| Viewer | Header fields | Binary |
|---|---|---|
| `image` (Image) | `binary: "jpeg"`, `width`, `height`, `encoding`, `scaled` (true when contrast-stretched) | JPEG file |
| `image` (CompressedImage) | `binary: "jpeg"`, `format`, `scaled: false`, `width`/`height` only when re-encoded | JPEG file |
| `pointcloud` | `binary: "points"`, `count`, `stride` (3 or 6), `total_points`, `valid_points`, `colour` (`rgb`, `rgba`, `intensity` or `null`) | little-endian float32, `count × stride` values: `x y z` or `x y z r g b` (colour 0–1) |
| `laserscan` | `angle_min`, `angle_increment` (already multiplied by the thinning step), `range_min`, `range_max`, `total_rays`, `ranges` | – |
| `imu` | `orientation {x,y,z,w}`, `angular_velocity {x,y,z}`, `linear_acceleration {x,y,z}`, `has_orientation`, `has_angular_velocity`, `has_linear_acceleration` | – |
| `jointstate` | `names`, `position`, `velocity`, `effort` (arrays may be empty) | – |
| `navsatfix` | `latitude`, `longitude`, `altitude`, `status`, `service`, `horizontal_accuracy_m`, `valid` | – |
| `range` | `range`, `min_range`, `max_range`, `field_of_view`, `radiation_type` (`ultrasound`, `infrared`, `unknown`) | – |
| `camerainfo` | `width`, `height`, `distortion_model`, `d`, `k` (9), `r` (9), `p` (12), `binning_x`, `binning_y`, `roi {x_offset, y_offset, width, height, do_rectify}` | – |

## Action execution

These routes send and cancel action goals. Together with the parameter-change and
service-call routes below, they are the only ways the gateway acts on ROS; there is no
route for publishing on topics.

### Access rules for the `POST` routes

A `POST` (goal, cancellation, parameter change, service call or conversation change) is accepted only if all
of the following hold; otherwise the status is `403` (or `415`/`422` for a body that is
not declared as JSON):

1. Changes are enabled (the server was not started with `--read-only`).
2. The TCP connection comes from a loopback address.
3. The `Host` header names `127.0.0.1`, `localhost` or `::1`.
4. An `Origin` header, if present, is a local origin.
5. `Content-Type` is `application/json`.
6. The header `X-SMPA-GUI-Token` carries the current session token.

`GET /session` is subject to rules 2–4.

### `GET /session` (also `GET /actions/session`)

`{"enabled": true, "token": "<session token>", "cancel_on_exit": true}`. The token is
random per gateway process and authorises every `POST` route (goals, parameters, services, conversation). In read-only mode
`enabled` is `false` and `token` is `null`. `/actions/session` is the original name.

### `GET /actions/servers`

`{"actions": [{"id", "name", "types", "servers", "clients", "available"}]}`: the graph's
action entries plus `available` (at least one server node is discovered).

### `GET /actions/interface?type=<pkg/action/Name>`

```json
{"type": "example_interfaces/action/Fibonacci",
 "goal": {"kind": "message", "type": "example_interfaces/action/Fibonacci_Goal",
          "fields": [{"name": "order", "schema": {"kind": "integer", "type": "int32",
                                                   "minimum": -2147483648, "maximum": 2147483647}}]},
 "goal_default": {"order": 0},
 "feedback": {"kind": "message", "...": "..."},
 "result": {"kind": "message", "...": "..."}}
```

Schemas are derived from the installed interface definition. `404` when the type is not
available in the gateway's ROS environment.

| `kind` | Other keys |
|---|---|
| `boolean` | `type` |
| `integer` | `type`, `minimum`, `maximum` |
| `float` | `type`, `limit` (largest magnitude, or `null`) |
| `string` | `type` (`string` or `wstring`), `max_length` (or `null`) |
| `array` | `item` (schema), `fixed_size` (or `null`), `max_size` (or `null`) |
| `message` | `type`, `fields: [{"name", "schema"}]` |

### `POST /actions/goals`

Body:

```json
{"action": "/fibonacci", "type": "example_interfaces/action/Fibonacci",
 "goal": {"order": 5}, "request_id": "0f8fad5bd9cb469fa16570867728950e"}
```

- `goal` may omit fields; they keep the interface defaults. Unknown fields are refused.
- `request_id` is 8–64 characters of `A–Z a–z 0–9 _ -`, chosen by the client once per
  deliberate submission. **A repeated `request_id` never sends a second goal**: the
  response is the first goal's record with `"duplicate": true` and status `200`.

Responses: `201` with `{"goal": <goal record>}` when the goal was sent. `422` with
`{"detail", "errors": [{"path": "source_time.sec", "message": "..."}]}` when the values do
not fit the Goal definition (nothing is sent). `409` when no action server is available
within two seconds. `404` when the action type is not available. `403` see access rules.

### `POST /actions/goals/{goal_id}/cancel`

Asks the action server to cancel an active goal (empty JSON body `{}`). `200` with the
updated record; the server's answer arrives later in `cancel_answer`. `404` unknown
goal, `409` goal not active.

### `GET /actions/goals`, `GET /actions/goals/{goal_id}`

`{"goals": [<goal record>, ...]}` newest first, or `{"goal": <goal record>}`. Records
exist for goals sent by this gateway process since it started (at most 200).

### Goal record

```json
{"goal_id": "010dd227-d1e0-4358-8d3d-fad4f2f248bb", "request_id": "...",
 "action": "/fibonacci", "type": "example_interfaces/action/Fibonacci", "goal": {"order": 5},
 "state": "succeeded", "created_at": 1794700546.39, "accepted_at": 1794700546.40,
 "finished_at": 1794700547.20,
 "feedback_count": 4, "feedback": [{"number": 4, "at": 1794700547.0, "message": {"sequence": [0, 1, 1, 2, 3, 5]}}],
 "result": {"sequence": [0, 1, 1, 2, 3, 5]}, "result_truncated": false, "error": null,
 "cancel_requested_at": null, "cancel_answer": null}
```

- `goal_id` is the ROS goal UUID. Times are seconds since the epoch on the gateway's clock.
- `feedback` holds the most recent 100 messages; `feedback_count` counts all. Feedback
  and result use the bounded JSON form of the echo stream.
- `cancel_answer`: `pending`, `accepted`, `rejected`, `unknown goal`, `goal already finished`,
  or `failed: ...`.

| `state` | Meaning |
|---|---|
| `sending` | Sent, waiting for the server to accept or reject |
| `accepted`, `executing`, `canceling` | Active, as reported by the server |
| `succeeded`, `aborted`, `canceled` | Finished, as reported by the server |
| `rejected` | The server refused the goal |
| `failed` | Sending or waiting failed inside the gateway (`error` explains) |
| `lost` | The action server disappeared while the goal was active; outcome unknown |

## Parameter management

### `GET /ros/parameters?timeout=<seconds, 0.2–5, default 1>`

Read-only overview of every visible node's parameters (hidden nodes are left out), used
by the ROS 2 Workspace. Each node is read exactly like `GET /ros/nodes/parameters`, up to
eight nodes at a time, so an unresponsive node costs one timeout and does not hide the
others. The GUI's own node is listed with `is_gateway: true` and `state: "unavailable"`
without being asked (it offers no parameter services).

```json
{"contract_version": "1.6", "timeout": 1.0,
 "nodes": [{"node": "/fixture_ns/fixture_parameters", "state": "ok", "is_gateway": false,
            "parameters": [<as in GET /ros/nodes/parameters>]},
           {"node": "/fixture_silent", "state": "not_responding", "is_gateway": false, "parameters": []},
           {"node": "/smpa_gui", "state": "unavailable", "is_gateway": true, "parameters": []}]}
```

`state` is `ok`, `not_responding`, `unavailable`, or `error` (with `detail`) when reading
that node failed in the gateway. Nodes keep the order of `GET /ros/nodes`. A timeout
outside 0.2–5 s gives `422`.

### `POST /ros/parameters/set`

```json
{"node": "/fixture_mgmt/settings", "atomic": true, "request_id": "param0123abcd",
 "changes": [{"name": "level", "type": "integer", "value": 6},
             {"name": "names", "type": "string_array", "value": ["x", "y"]}]}
```

`type` is one of `bool`, `integer`, `double`, `string`, `byte_array` (list of 0–255),
`bool_array`, `integer_array`, `double_array`, `string_array`; at most 50 changes, each
parameter once. The gateway first reads the node's parameters and descriptors afresh
and checks every change: the parameter exists, is not read-only, keeps its type unless
dynamic typing allows a change, the value fits the type, and integer/floating-point
ranges with their step are respected. Any problem answers `422`
`{"detail", "errors": [{"path", "message"}]}` and nothing is sent. A node that does not
answer answers `409` and nothing is sent. Otherwise the changes are sent once through
`set_parameters_atomically` (`atomic: true`) or `set_parameters` and the answer is:

```json
{"change": {"change_id": "param0123abcd", "node": "/fixture_mgmt/settings", "atomic": true,
  "outcome": "applied|rejected|partial|unknown|unavailable", "requested_at": 1794000000.0,
  "results": [{"name": "level", "type": "integer", "value": 6, "previous_type": "integer",
               "previous_value": 4, "successful": true, "reason": ""}],
  "parameters": [<parameters read back, as in GET /ros/nodes/parameters>], "read_back_state": "ok"}}
```

`unknown` means the node did not answer in time: the changes may or may not have been
applied; they are not retried (`successful` is `null`). Repeating a `request_id`
returns the first record with `"duplicate": true` and changes nothing.

### `GET /ros/parameters/changes`

`{"changes": [<change record>, ...]}` made through this GUI process, newest first (at most 200).

## Service management

### `GET /ros/services`

```json
{"services": [{"name": "/fixture/add_two_ints", "types": ["example_interfaces/srv/AddTwoInts"],
               "kind": "other", "hidden": false,
               "providers": ["/fixture_mgmt/services"], "clients": []}]}
```

`kind` is `parameter` (standard parameter services), `action` (action plumbing),
`type_description`, or `other`. Providers and clients are node names.

### `GET /ros/services/interface?type=<pkg/srv/Name>`

`{"type", "request": <schema>, "request_default": {...}, "response": <schema>}` with
schemas as for action goals. `404` when the type is not available in the GUI's environment.

### `POST /ros/services/call`

`{"service": "/fixture/add_two_ints", "type": "example_interfaces/srv/AddTwoInts",
"request": {"a": 2, "b": 40}, "request_id": "svc0123abcd", "timeout": 10}` (timeout
0.5–60 s). Invalid values answer `422` with `errors`; an unavailable type `404`; no
provider within two seconds `409` (nothing sent). Otherwise the request is sent once
and the answer is the call record:

```json
{"call": {"call_id": "…", "request_id": "svc0123abcd", "service": "…", "type": "…", "request": {…},
          "state": "succeeded|timed_out|server_lost|failed", "response": {"sum": 42}, "error": null,
          "timeout": 10, "created_at": 1794000000.0, "finished_at": 1794000000.1}}
```

`timed_out` (no response in time) and `server_lost` (the provider left the ROS graph
while waiting) answer `504`: the outcome is unknown and the call is not repeated.
Repeating a `request_id` returns the first record with `"duplicate": true`.

### `GET /ros/services/calls`, `GET /ros/services/calls/{call_id}`

`{"calls": [<call record>, ...]}` newest first (at most 200), or `{"call": <call record>}`.

## SMPA sessions (read-only, optional)

These routes read SMPA's memory directory (`--memory-root`, `SMPA_GUI_MEMORY_ROOT`, or
`memory/` beside the source checkout). They never write. With SMPA integration
disabled (`--no-smpa`) `/smpa/status` says `"enabled": false` and the other routes
answer `404` saying so. When no memory directory is configured,
`/smpa/status` says so and the other routes answer `404` with an explanation. The data
sources behind each field are listed in [SMPA_CONTRACTS.md](SMPA_CONTRACTS.md).

### `GET /smpa/status`

`{"enabled": true, "configured": true, "memory_root": "/…/smpa-ros/memory", "demonstration": false}`.
`demonstration` is true when the directory carries the GUI marker `.smpa-gui-demo`
(written only by `test/fixtures/smpa_memory.py --demo`).

### `GET /smpa/sessions`, `GET /smpa/sessions/{session_id}`

`{"memory_root", "demonstration", "sessions": [<session>, ...]}` newest first, or `{"session": <session>}`.

```json
{"session_id": "11111111-1111-4111-8111-111111111111", "name": "fixture_all_senses",
 "directory": "2026-11-15_100000_000000+0100_fixture_all_senses_1111…", "path": "runs/…",
 "kind": "robot", "status": "completed", "activity": "ended",
 "started_at": "2026-11-15T10:00:00+01:00", "completed_at": "…", "completed_cycles": 1,
 "modalities": {"vision": true, "hear": true, "read": true},
 "camera_topic": "/tiago/camera/color/image_raw",
 "read_session": {"path": "/…/runs/…_conversation_2222…", "session_id": "2222…"},
 "ros": {"visible": false, "names": [], "nodes": [], "listen_status_topics": []},
 "metadata_error": null}
```

| Field | Values |
|---|---|
| `kind` | `robot`, `conversation`, `legacy_archive` (the `memory/legacy` folder, id `legacy-archive`), `unknown` (metadata missing or unreadable; `metadata_error` explains) |
| `status` | As recorded by SMPA: `running`, `completed`, `interrupted`, `failed`, or `null` |
| `activity` | `active` (running, ROS names under `/run_<uuid>` visible), `running_not_visible` (running, none visible), `running_unverified` (running, liveness cannot be checked: conversation sessions, or no ROS graph), `ended`, `unknown` |
| `modalities` | Per Sense modality `true`, `false`, or `null` when the setting is malformed |
| `ros.listen_status_topics` | Status topics of this run's Listen action(s), for the live Hear state |

### `GET /smpa/sessions/{session_id}/observations?modality=`

`{"session_id", "revision", "observations": [<observation>, ...], "truncated", "warnings"}`
in chronological order (oldest first), at most the newest 2000. `modality` filters to
`vision`, `hear`, `read` or `sense`. `revision` changes whenever a file of the session
changes; clients can skip redrawing when it is unchanged.

```json
{"id": "9f2c…", "session_id": "1111…", "modality": "vision", "kind": "scene_description",
 "recorded": true, "timestamp": "2026-11-15T09:00:05.250000+00:00", "t": 1794733205.25,
 "timestamp_source": "file name (UTC)", "status": "generated",
 "text": "{\"scene\": \"A red test card fills the view.\"}", "text_truncated": false,
 "image": "/api/v1/smpa/sessions/1111…/files?path=observations/vision/…png", "audio": null,
 "source": "observations/vision/2026-11-15_09-00-05_250000Z_sense_output.md",
 "sense_record": "state/cycle_000001/sense.json", "cycle": null, "epistemic_status": null,
 "provenance": {"camera": {"topic": "…", "received_at": "…", "frame_id": "…"}, "image_size": [64, 48]}}
```

| `modality` | `kind` | Content |
|---|---|---|
| `vision` | `scene_description` | Description text; `image` when its frame was saved |
| `vision` | `camera_frame` | Frame without description (`status` `evidence_without_description`) or refinement's frozen image (`frozen_for_refinement`) |
| `vision` | `focused_observation` | Answer text; `provenance.target`, `question`, `image_sha256`, `model_identifier` |
| `hear` | `audio_transcript` | Transcript text; `audio` recording; `provenance` holds the transcript fields and `duration_s` |
| `read` | `user_message` | Message text; `epistemic_status`; `provenance.role`, `message_index`, `conversation_session_id` |
| `sense` | `sense_record` | One Sense invocation: `status`, `cycle`, error text, `provenance.source_observations`, `read_cursor` |

- `recorded` is always `true` here: everything comes from SMPA's files. Live data is only
  available through the sensor and echo streams.
- `timestamp_source` says where the time came from: `file name (UTC)`, `file name (local
  time; run's start offset applied)`, `PNG smpa.camera received_at`, `transcript
  Timestamp`, `Sense record started_at`, or `file modification time`.
- `sense_record` is set only where SMPA recorded the link.

### `GET /smpa/sessions/{session_id}/files?path=<relative path>`

One recorded file of the session (`.png`, `.wav`, `.md`, `.json`, `.txt`) with its media
type; byte ranges are supported. Paths outside the session directory, through links
leading outside, or with other suffixes give `404`.

## SMPA conversation

The GUI implements no conversation logic. These routes start (on demand) and relay to
SMPA's own `smpa_ros.conversation_web` server, or to an external one when
`conversation_url` is configured. The `POST` routes follow the
[access rules](#access-rules-for-the-post-routes) of the action routes, with the same
session token; with `--read-only` they answer `403`. With SMPA integration disabled
(`--no-smpa`) every `/smpa/...` Dashboard route answers `404` saying so. Each `POST` answers with
`{"conversation": <status>}`; on failure the status code says why and `detail` carries
SMPA's or the GUI's explanation, together with the current `conversation`.

| Status | Meaning |
|---|---|
| `403` | Not local, no token, or read-only |
| `404` | Unknown session (resume) or no SMPA agent for that model source |
| `409` | Busy with another request; no conversation active; SMPA refused (for example missing weights); server prerequisites missing; port in use |
| `502` | SMPA's server failed: it could not start (see `log`), or inference failed (the message is saved) |
| `503`, `504` | SMPA's server is unreachable or did not start in time |

### `GET /smpa/layout`

The effective paths: `smpa_root`, `config`, `python`, `model_root`, `memory_root`,
`conversation_port`, `conversation_url`, `mock_graphs`, `smpa_layout` (`packages`, `flat`
or `null` when no SMPA checkout is configured), `files` (`conversation`, `sense`,
`belief`, `plan`: each a list of candidate paths relative to a run directory, the first
existing one is used), the layout file used (`source`), and
`python_present`, `config_present`, `conversation_server_present`.

### `GET /smpa/models`

```json
{"models": [{"source": "api:openai", "kind": "api", "name": "openai", "provider": "openai", "backend": "api",
             "model_id": "gpt-4.1-mini", "revision": null,
             "description": "API provider openai, model gpt-4.1-mini, endpoint https://api.openai.com/v1, credentials from OPENAI_API_KEY.",
             "availability": {"state": "likely|available|unavailable|unknown", "verified": false, "detail": "…"},
             "default": false, "modelling_agent": false, "agent_id": null, "label": "…(when verified)"}],
 "problems": [], "config": "/…/config.yaml", "verified_by": "gui_precheck|conversation_server"}
```

`source` is SMPA's name for the choice. Until SMPA's server runs, availability is the
GUI's precheck (`verified: false`); afterwards it is SMPA's verdict and `agent_id` is
SMPA's agent id. Secrets are never returned, only the name of the credential variable.

### `GET /smpa/conversation`

The conversation status:

```json
{"conversation": {"mode": "managed|external", "url": "http://127.0.0.1:8766",
  "phase": "stopped|starting|ready|busy|failed|unreachable",
  "operation": {"kind": "start|resume|model|message|stop", "elapsed_s": 3.1, "text": "…(message)"},
  "pid": 1234, "error": null, "exit_code": null, "run_id": "…", "session_id": "…",
  "active_agent": {"id": "api:openai:…", "source": "api:openai", "label": "API · openai · …"},
  "agents": [{"source": "…", "id": "…", "label": "…", "available": true, "availability": "…"}],
  "messages": [{"index": 1, "role": "user|assistant", "content": "…", "thinking": null,
                "agent": {"source": "…", "label": "…", "provider": "…", "model_id": "…", "backend": "…", "model_revision": null}}],
  "modelling_error": null, "prerequisites_missing": [], "writes_enabled": true, "log": ["…server output…"]}}
```

`run_id` (equal to `session_id` of `/smpa/sessions`) is the active conversation. While
SMPA generates a reply, `phase` is `busy` and the last known messages are returned.

### `POST /smpa/conversation/start` `{"source": "api:openai", "request_id": "…"}`

Starts a new SMPA conversation run, then selects the model (`source` optional: SMPA's
configured default). If SMPA's server is not running it is started, which creates the
run. Repeating a `request_id` returns the first outcome and starts nothing.

### `POST /smpa/conversation/resume` `{"session_id": "…", "request_id": "…", "source": null}`

Reopens a recorded conversation session in SMPA (starting the server with `--resume`
or switching a running one). SMPA restores the model recorded in the run.

### `POST /smpa/conversation/model` `{"source": "local:qwen3-4b"}`

Switches the active conversation's model; SMPA records the change in the run.

### `POST /smpa/conversation/messages` `{"text": "…", "request_id": "…"}`

Sends one message and answers when SMPA has saved it and its reply (this can take
minutes for local models). Repeating a `request_id` does not send twice.

### `POST /smpa/conversation/stop` `{}`

Stops the server the GUI started (SIGINT; SMPA finalizes the run). For an external
server only the GUI's state is cleared.

### `GET /smpa/sessions/{session_id}/conversation`

The recorded transcript at the first existing `files.conversation` candidate in that run,
read-only: `{"state": "ok|partial|missing|unavailable", "path", "configured_path"` (the
candidate used), `"candidates", "fallback"` (true when it is not the first candidate),
`"revision",
"active", "messages": [as above], "problem"}`. `partial` means the complete messages
before an incomplete or invalid entry.

### `GET /smpa/graphs/{view}?source=real|mock&session_id=`

`view` is `sense`, `belief` or `plan`. `real` reads the first existing `files.<view>`
candidate in the session's run (`configured_path` names it, `candidates` lists all,
`fallback` is true when it is not the first; when none exists the first is reported as
`missing`);
`mock` reads `<mock_graphs>/<view>.ttl` and is always marked `"mock": true`.

```json
{"view": "belief", "source": "real", "mock": false, "session_id": "…",
 "configured_path": "world_model/belief.ttl", "path": "/…/runs/…/world_model/belief.ttl",
 "candidates": ["world_model/belief.ttl", "belief-state.ttl"], "fallback": false,
 "state": "ok|missing|empty|malformed|unavailable", "revision": "sha1 prefix of the content",
 "modified_at": "…", "size": 123, "triples": 6, "truncated": false, "error": null,
 "prefixes": {"ex": "http://example.org/"},
 "nodes": [{"id": "http://example.org/cup", "label": "ex:cup", "iri": "…", "blank": false,
            "literals": [{"predicate": "…", "predicate_label": "ex:colour", "value": "red", "datatype": null, "language": null}]}],
 "edges": [{"id": "…", "source": "…", "target": "…", "predicate": "…", "label": "ex:on"}]}
```

No vocabulary is interpreted. `revision` changes exactly when the file content changes,
so clients redraw only then. At most 5000 triples are returned (`truncated`).
