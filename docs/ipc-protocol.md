# KIVO Local IPC Protocol (Stage 2)

This document describes the local Unix-domain socket bridge between the KIVO
Python backend and a future Quickshell/QML frontend. The frontend itself is not
part of this stage.

## Scope

Implemented in this stage:

- A Unix-domain stream socket at `$XDG_RUNTIME_DIR/kivo/kivo.sock`.
- Newline-delimited, versioned JSON requests, responses and events.
- Six validated frontend operations.
- Real backend event producers for assistant/audio/agent state, Drive progress,
  screen-context metadata, and task history.
- Private socket permissions, stale-socket recovery, duplicate-server
  prevention, multiple clients, and graceful cleanup.

Explicitly not implemented:

- Any TCP listener or network transport.
- Shell execution, filesystem writes, or arbitrary backend method invocation.
- API keys, passwords, voiceprints, private memory, OCR text, captured images,
  or tool parameters/results/error text in any response or event.
- Confirmation resolution. The confirmation gate remains unbound and
  fail-closed until a real approval UI exists.
- Quickshell, Dashboard, Dynamic Notch, anime character, lip-sync, installer,
  or README changes.

## Transport and security

| Property | Value |
| --- | --- |
| Socket type | `AF_UNIX`, `SOCK_STREAM` |
| Default path | `$XDG_RUNTIME_DIR/kivo/kivo.sock` |
| Socket mode | `0600` (user read/write only) |
| Directory mode | `0700` (the `kivo` directory must be user-owned/private) |
| Runtime validation | `$XDG_RUNTIME_DIR` must exist, be absolute, be a directory, be owned by the current user, and have no group/other permissions |
| Framing | One UTF-8 JSON object per line (`\n`) |
| Maximum message | 65,536 bytes per line, excluding the trailing newline |
| Duplicate servers | Non-blocking `flock` on `kivo.sock.lock` |
| Stale socket | Removed only after a failed connect probe and only if it is a socket owned by the current user |
| Non-socket file at the path | Refused; never overwritten |

There is no application-level authentication. Access control is the Unix socket
and directory permissions, so only the current user can connect. Do not move the
socket into a shared directory.

## Framing and versioning

Each frame is a single JSON object followed by `\n`. JSON string escaping means
a frame cannot contain a raw embedded newline. The server reads at most
`MAX_MESSAGE_BYTES + 2` bytes per frame; an oversized frame receives a
`message_too_large` response and the connection is closed.

`version` must be the integer `1`. Booleans, strings, floats and other values
are rejected with `unsupported_version`. Unknown top-level fields are rejected
with `invalid_request`.

## Message envelopes

### Request

```json
{
  "version": 1,
  "type": "request",
  "id": "req-1",
  "operation": "get_status",
  "params": {}
}
```

- `id`: non-empty string, at most 128 characters, no NUL byte. The server
  echoes it in the response.
- `operation`: one of the operations listed below.
- `params`: optional JSON object. Operations other than `subscribe_events` do
  not accept parameters.

### Success response

```json
{
  "version": 1,
  "type": "response",
  "id": "req-1",
  "ok": true,
  "result": {}
}
```

### Error response

```json
{
  "version": 1,
  "type": "response",
  "id": "req-1",
  "ok": false,
  "error": {
    "code": "invalid_params",
    "message": "operation 'get_status' does not accept parameters"
  }
}
```

When a frame cannot be parsed far enough to recover an ID, `id` is `null`.

### Event

```json
{
  "version": 1,
  "type": "event",
  "event": "drive_progress",
  "sequence": 7,
  "data": {}
}
```

`sequence` is a monotonically increasing integer assigned by the server process
to every published event. It is shared across clients. Filters can cause gaps
in the sequence numbers a particular client sees.

The server emits compact JSON; examples below are formatted for readability.

## Operations

Only these operations are accepted:

| Operation | Params | Result |
| --- | --- | --- |
| `get_status` | none | Current runtime status |
| `get_current_task` | none | Newest unfinished task or `null` |
| `get_dashboard_data` | none | Status, current task, and recent task history |
| `subscribe_events` | optional `{"events": [...]}` | Subscription confirmation |
| `unsubscribe_events` | none | Subscription disabled |
| `cancel_active_task` | none | Cancellation result |

Unknown operations are rejected with `unsupported_operation` before any backend
method is called.

### `get_status`

```json
{
  "version": 1,
  "type": "request",
  "id": "1",
  "operation": "get_status"
}
```

```json
{
  "version": 1,
  "type": "response",
  "id": "1",
  "ok": true,
  "result": {
    "service": "kivo",
    "version": "0.1.0",
    "protocol_version": 1,
    "pid": 12345,
    "assistant_state": "idle",
    "assistant_detail": "backend started",
    "audio_state": "unavailable",
    "audio_detail": "audio pipeline is not initialized",
    "agent_state": "idle",
    "agent_detail": "no active task",
    "uptime_seconds": 12.345,
    "active_task": false,
    "drive": {
      "active": false,
      "goal": "",
      "action": "",
      "reason": "",
      "elapsed": 0.0
    }
  }
}
```

Assistant states: `idle`, `listening`, `thinking`, `speaking`, `driving`,
`error`, `wake_word`, `voice_command`, `cancellation`, `full_screen_mode`,
`settings_mode`, `dashboard_open`, `mute`.

Audio states: `unavailable`, `idle`, `listening`, `capturing`, `speaking`,
`muted`, `error`. Audio remains `unavailable` in this stage; no microphone or
audio pipeline is started.

Agent states: `idle`, `planning`, `working`, `waiting`, `error`.

### `get_current_task`

```json
{
  "version": 1,
  "type": "request",
  "id": "2",
  "operation": "get_current_task"
}
```

```json
{
  "version": 1,
  "type": "response",
  "id": "2",
  "ok": true,
  "result": {
    "task": {
      "task_id": "task-abc123",
      "goal": "Prepare the release notes",
      "workspace_id": "",
      "created_at": 1760000000.0,
      "current_step": 1,
      "total_steps": 3,
      "status": "running",
      "success": false,
      "summary": "",
      "steps": [
        {"id": 1, "description": "Collect changes", "status": "completed", "is_critical": true},
        {"id": 2, "description": "Draft notes", "status": "running", "is_critical": true},
        {"id": 3, "description": "Ask for approval", "status": "pending", "is_critical": true}
      ]
    }
  }
}
```

When no task is active:

```json
{"task": null}
```

Task status values: `pending`, `running`, `completed`, `failed`, `cancelled`.
Step status values: `pending`, `running`, `completed`, `failed`, `skipped`,
`rolled_back`.

The snapshot intentionally excludes tool names, tool parameters, action results,
and error text. A failed task's `summary` is reduced to a generic message such
as `Task failed at step 2.` even when the underlying tool returned sensitive
output.

### `get_dashboard_data`

```json
{
  "version": 1,
  "type": "request",
  "id": "3",
  "operation": "get_dashboard_data"
}
```

```json
{
  "version": 1,
  "type": "response",
  "id": "3",
  "ok": true,
  "result": {
    "status": {"service": "kivo", "...": "as in get_status"},
    "current_task": null,
    "task_history": []
  }
}
```

`task_history` contains at most the 10 most recently finished tasks. This is a
small, UI-safe snapshot, not a memory or telemetry dump.

### `subscribe_events`

Subscribe to every event:

```json
{
  "version": 1,
  "type": "request",
  "id": "4",
  "operation": "subscribe_events",
  "params": {}
}
```

Subscribe to a filter:

```json
{
  "version": 1,
  "type": "request",
  "id": "4",
  "operation": "subscribe_events",
  "params": {"events": ["drive_started", "drive_progress", "drive_completed"]}
}
```

```json
{
  "version": 1,
  "type": "response",
  "id": "4",
  "ok": true,
  "result": {
    "subscribed": true,
    "events": ["drive_completed", "drive_progress", "drive_started"]
  }
}
```

- Omitted or empty `params` subscribes to all events.
- `events` must be a non-empty list of known event names, at most 32 entries,
  with no duplicates. Invalid names are rejected with `invalid_params`.
- Each client has its own subscription state.

### `unsubscribe_events`

```json
{
  "version": 1,
  "type": "request",
  "id": "5",
  "operation": "unsubscribe_events"
}
```

```json
{
  "version": 1,
  "type": "response",
  "id": "5",
  "ok": true,
  "result": {
    "subscribed": false,
    "events": []
  }
}
```

### `cancel_active_task`

```json
{
  "version": 1,
  "type": "request",
  "id": "6",
  "operation": "cancel_active_task"
}
```

```json
{
  "version": 1,
  "type": "response",
  "id": "6",
  "ok": true,
  "result": {
    "cancelled": true,
    "message": "Cancelling: Prepare the release notes"
  }
}
```

`cancelled` reports whether a task was active when the request arrived. The
orchestrator sets the task's cancellation event and stops the shared Drive
session. Running tasks stop at the next cancellation-aware step boundary;
tasks that were created but never started are finalized immediately as
`cancelled`.

## Events

All events use the event envelope described above and carry a `data` object.

| Event | Producer | Status |
| --- | --- | --- |
| `assistant_state_changed` | `RuntimeState` | Emitted on real state changes |
| `audio_state_changed` | `RuntimeState` | Emitted on real state changes; initial state is `unavailable` |
| `user_transcript` | — | Protocol-reserved; no voice session is bound yet |
| `assistant_response` | — | Protocol-reserved; no model session is bound yet |
| `drive_started` | `DriveSession` listener | Emitted when sustained desktop work starts |
| `drive_progress` | `DriveSession` listener | Emitted when the Drive action changes |
| `drive_completed` | `DriveSession` listener | Emitted when Drive stops without cancellation/failure |
| `drive_failed` | `DriveSession` listener | Emitted when the stop reason indicates failure |
| `drive_cancelled` | `DriveSession` listener | Emitted when the stop reason indicates cancellation |
| `confirmation_required` | — | Protocol-reserved; the confirmation gate is unbound and fail-closed |
| `screen_context_updated` | `ScreenContextStore` listener | Emitted on remember/clear; metadata only |
| `task_history_updated` | `TaskOrchestrator` listener | Emitted on task create/progress/finish |
| `agent_state_changed` | `RuntimeState` via task listener | Emitted when task activity changes |
| `error` | — | Protocol-reserved for out-of-band backend errors |

Request failures are returned as error responses, not `error` events.

### `assistant_state_changed`

```json
{
  "version": 1,
  "type": "event",
  "event": "assistant_state_changed",
  "sequence": 1,
  "data": {
    "state": "thinking",
    "previous": "idle",
    "detail": "processing user request"
  }
}
```

`previous` is `null` for the initial state published at startup.

### `audio_state_changed`

Same shape as `assistant_state_changed`. Example initial state:

```json
{"state": "unavailable", "previous": null, "detail": "audio pipeline is not initialized"}
```

### `agent_state_changed`

Same shape. Agent state is `working` while a task is active and `idle` when no
task remains active.

### `drive_started` / `drive_progress`

```json
{
  "version": 1,
  "type": "event",
  "event": "drive_started",
  "sequence": 2,
  "data": {
    "goal": "Prepare the release notes",
    "action": "Starting…"
  }
}
```

`drive_progress` has the same data shape and is emitted when the action changes
while the session stays active.

### `drive_completed` / `drive_failed` / `drive_cancelled`

```json
{
  "version": 1,
  "type": "event",
  "event": "drive_cancelled",
  "sequence": 3,
  "data": {
    "goal": "Prepare the release notes",
    "reason": "cancelled by user"
  }
}
```

The terminal event is chosen from the actual Drive stop reason:

- cancellation, shutdown, or stopped → `drive_cancelled`
- fail, error, abort, or denied → `drive_failed`
- anything else → `drive_completed`

### `task_history_updated`

```json
{
  "version": 1,
  "type": "event",
  "event": "task_history_updated",
  "sequence": 4,
  "data": {
    "reason": "created",
    "active_task": true,
    "current_task": {"task_id": "task-abc123", "...": "task snapshot"},
    "task_history": []
  }
}
```

`reason` is `created`, `progress`, or `finished`. `current_task` is `null` when
no task is active; `task_history` uses the same safe snapshot as
`get_dashboard_data`.

### `screen_context_updated`

```json
{
  "version": 1,
  "type": "event",
  "event": "screen_context_updated",
  "sequence": 5,
  "data": {
    "cleared": false,
    "active_app": "Alacritty",
    "active_title": "shell",
    "is_terminal": true,
    "is_browser": false,
    "source": "screen",
    "age_seconds": 0.42
  }
}
```

When the store is cleared: `cleared` is `true` and the remaining fields are
empty/false/zero. OCR text, captured images, frame data, terminal working
directory, and window IDs are never included.

## Error codes

| Code | Meaning |
| --- | --- |
| `message_too_large` | Frame exceeds 65,536 bytes; connection closes after the error |
| `invalid_json` | Invalid UTF-8, empty frame, or malformed JSON |
| `invalid_request` | Not an object, unknown field, bad ID, or bad operation type |
| `unsupported_version` | `version` is not the integer `1` |
| `invalid_type` | `type` is not `request` |
| `unsupported_operation` | Operation is not in the allowed set |
| `invalid_params` | Parameters are not accepted or fail validation |
| `internal_error` | Backend handler raised; details are not leaked |

Protocol errors do not close the connection except for `message_too_large`.
The client can continue sending requests after a normal validation error.

## Subscription and delivery semantics

- A connection starts unsubscribed.
- Subscriptions are per client and survive until `unsubscribe_events` or
  disconnect.
- Events are queued to each subscribed client in a bounded queue of 256 frames.
  If a client cannot keep up, the server disconnects it rather than blocking
  backend workers.
- Delivery is best-effort; there is no replay buffer or acknowledgement.
- Requests on one connection are processed in order by that connection's reader
  thread. Different clients are independent.
- Initial state events are published during startup before any client can
  subscribe. A newly connected client should call `get_status` for the current
  snapshot, then subscribe to future changes.

## Lifecycle

Startup (existing `KivoApp` order):

1. Configuration, memory, brain, screen-context store, actions, task
   orchestrator, Drive session, confirmation service.
2. Validate the runtime directory and acquire the startup lock.
3. Recover a stale socket if safe, bind, set mode `0600`, and start accepting.
4. Publish initial assistant/audio/agent/screen state.

Shutdown:

1. Cancel active tasks and stop Drive while the IPC server is still running, so
   resulting cancellation events can be delivered.
2. Remove backend listeners and unbind the confirmation gate.
3. Stop accepting clients, close active connections, remove the socket, release
   the lock.
4. Shutdown is idempotent and safe after partial startup.

## Example session

With KIVO running in one terminal:

```bash
.venv/bin/python - <<'PY'
import json
import os
import socket

path = os.path.join(os.environ["XDG_RUNTIME_DIR"], "kivo", "kivo.sock")
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(path)
stream = sock.makefile("rwb")

def send(payload):
    stream.write((json.dumps(payload) + "\n").encode())
    stream.flush()

def receive():
    return json.loads(stream.readline())

send({"version": 1, "type": "request", "id": "1", "operation": "get_status"})
print(json.dumps(receive(), indent=2))

send({"version": 1, "type": "request", "id": "2",
      "operation": "subscribe_events",
      "params": {"events": ["drive_started", "drive_progress", "drive_completed"]}})
print(json.dumps(receive(), indent=2))

# The next line received on this socket is an event, once backend work starts.
stream.close()
sock.close()
PY
```

## Launch, check and test commands

```bash
# Environment check (also prints the IPC socket path)
.venv/bin/python -m kivo --check

# Start the backend; Ctrl+C performs graceful shutdown
.venv/bin/python -m kivo
```

```bash
# IPC tests only (temporary socket paths, never the real runtime socket)
.venv/bin/python -m unittest tests.test_ipc -v

# Full suite
.venv/bin/python -m unittest discover -s tests
```

Tests construct `KivoApp(socket_path=...)` or `IPCServer(path, handlers=...)`
with a unique temporary directory. Overriding the path is intended for tests
and embedding; normal use should rely on the validated default path.

## Compatibility

`PROTOCOL_VERSION` is `1`. Additive fields may appear within version 1; clients
must ignore unknown response/event fields. A breaking envelope change requires a
new protocol version. Requests with unknown top-level fields are rejected, so
clients must not send speculative fields.
