# Gateway API, contract version 1.1

Base URL: `http://127.0.0.1:8765/api/v1`. All routes are read-only (HTTP `GET` or
WebSocket) except the two `POST` routes under [Action execution](#action-execution).
Version 1.1 added the `/actions/...` routes; nothing from 1.0 changed. Every JSON response contains `contract_version`. ROS names are passed as
URL-encoded query parameters because they contain slashes. A machine-readable
description of the HTTP routes is served at `/api/openapi.json`.

Errors use HTTP status codes with a JSON body `{"detail": "..."}`:
`404` unknown topic, `415` data that cannot be drawn, `503` ROS gateway unavailable,
`504` no data in time.

## Snapshots

### `GET /health`

```json
{"contract_version": "1.1", "status": "ok",
 "ros": {"ok": true, "node": "/smpa_gui", "domain_id": 0, "rmw": "rmw_fastrtps_cpp", "distro": "jazzy"},
 "active_subscriptions": [{"topic": "/scan", "type": "sensor_msgs/msg/LaserScan", "viewers": 1}]}
```

`active_subscriptions` lists what the gateway is subscribed to at this moment.

### `GET /ros/graph`

```json
{"contract_version": "1.1", "stamp": 1794679890.5,
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
{"contract_version": "1.1", "node": "/talker", "state": "ok",
 "parameters": [{"name": "use_sim_time", "type": "bool", "value": false,
                 "description": "", "read_only": false}]}
```

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

These routes are the only way the gateway acts on ROS. They send and cancel action
goals; there is no route for publishing, service calls or parameter writes.

### Access rules for the `POST` routes

A `POST` is accepted only if all of the following hold; otherwise the status is `403`
(or `415`/`422` for a body that is not declared as JSON):

1. Action execution is enabled (the server was not started with `--read-only`).
2. The TCP connection comes from a loopback address.
3. The `Host` header names `127.0.0.1`, `localhost` or `::1`.
4. An `Origin` header, if present, is a local origin.
5. `Content-Type` is `application/json`.
6. The header `X-SMPA-GUI-Token` carries the current session token.

`GET /actions/session` is subject to rules 2–4.

### `GET /actions/session`

`{"enabled": true, "token": "<session token>", "cancel_on_exit": true}`. The token is
random per gateway process. In read-only mode `enabled` is `false` and `token` is `null`.

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
