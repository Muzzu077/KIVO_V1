"""Versioned JSON protocol for the KIVO Unix-domain socket.

Framing
    One JSON object per UTF-8 line, terminated by ``\\n``.  A message's JSON
    text may not exceed ``MAX_MESSAGE_BYTES``.

Client request
    {"version": 1, "type": "request", "id": "req-1",
     "operation": "get_status", "params": {}}

Server response
    {"version": 1, "type": "response", "id": "req-1", "ok": true,
     "result": {...}}

Server event
    {"version": 1, "type": "event", "event": "drive_progress",
     "sequence": 7, "data": {...}}

Errors are responses with ``ok`` false and an ``error`` object containing
``code`` and ``message``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1

# A full line, excluding the trailing newline, must not exceed this.
MAX_MESSAGE_BYTES = 64 * 1024
MAX_REQUEST_ID_LENGTH = 128
MAX_PARAM_EVENTS = 32

REQUEST_OPERATIONS = frozenset({
    "get_status",
    "get_current_task",
    "get_dashboard_data",
    "subscribe_events",
    "unsubscribe_events",
    "cancel_active_task",
    "audio_get_status",
    "audio.get_status",
    "audio_list_devices",
    "audio.list_devices",
    "audio_select_device",
    "audio.select_device",
    "audio_start_capture",
    "audio.start_capture",
    "audio_stop_capture",
    "audio.stop_capture",
    "audio_cancel_capture",
    "audio.cancel_capture",
    "stt_get_status",
    "stt.get_status",
    "stt_get_history",
    "stt.get_history",
    "stt_get_result",
    "stt.get_result",
    "stt_update_config",
    "stt.update_config",
    "stt_cancel",
    "stt.cancel",
    "conversation_get_status",
    "conversation.get_status",
    "conversation_get_history",
    "conversation.get_history",
    "conversation_submit",
    "conversation.submit",
    "conversation_cancel",
    "conversation.cancel",
    "conversation_clear",
    "conversation.clear",
    "conversation_update_config",
    "conversation.update_config",
    "conversation_list_models",
    "conversation.list_models",
    "conversation_test_connection",
    "conversation.test_connection",
})

EVENT_NAMES = (
    "assistant_state_changed",
    "audio_state_changed",
    "user_transcript",
    "assistant_response",
    "drive_started",
    "drive_progress",
    "drive_completed",
    "drive_failed",
    "drive_cancelled",
    "confirmation_required",
    "screen_context_updated",
    "task_history_updated",
    "agent_state_changed",
    "audio_device_changed",
    "audio.device_changed",
    "audio_capture_started",
    "audio.capture_started",
    "audio_capture_stopped",
    "audio.capture_stopped",
    "audio_capture_completed",
    "audio.capture_completed",
    "audio_capture_error",
    "audio.capture_error",
    "stt_status_changed",
    "stt.status_changed",
    "stt_transcription_started",
    "stt.transcription_started",
    "stt_transcription_completed",
    "stt.transcription_completed",
    "stt_transcription_empty",
    "stt.transcription_empty",
    "stt_transcription_failed",
    "stt.transcription_failed",
    "conversation_state_changed",
    "conversation.state_changed",
    "conversation_user_message",
    "conversation.user_message",
    "conversation_response_started",
    "conversation.response_started",
    "conversation_response_delta",
    "conversation.response_delta",
    "conversation_response_completed",
    "conversation.response_completed",
    "conversation_response_failed",
    "conversation.response_failed",
    "conversation_cancelled",
    "conversation.cancelled",
    "agent_tool_started",
    "agent.tool_started",
    "agent_tool_completed",
    "agent.tool_completed",
    "error",
)
EVENT_NAME_SET = frozenset(EVENT_NAMES)


class ProtocolError(Exception):
    """A request that must be rejected by the server."""

    def __init__(self, code: str, message: str, *, close: bool = False,
                 request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.close = close
        self.request_id = request_id


@dataclass(frozen=True)
class Request:
    request_id: str
    operation: str
    params: dict[str, Any]


def decode_request(body: bytes) -> Request:
    """Parse and validate one request body (without the newline)."""
    if len(body) > MAX_MESSAGE_BYTES:
        raise ProtocolError(
            "message_too_large",
            f"message exceeds {MAX_MESSAGE_BYTES} bytes",
            close=True,
        )

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("invalid_json", "message is not valid UTF-8")
    if not text.strip():
        raise ProtocolError("invalid_json", "empty message")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid_json", f"malformed JSON: {exc.msg}")

    if not isinstance(payload, dict):
        raise ProtocolError("invalid_request", "message must be a JSON object")

    unknown_keys = set(payload) - {"version", "type", "id", "operation", "params"}
    if unknown_keys:
        raise ProtocolError(
            "invalid_request",
            "unknown top-level field(s): " + ", ".join(sorted(unknown_keys)),
        )

    version = payload.get("version")
    if (not isinstance(version, int) or isinstance(version, bool)
            or version != PROTOCOL_VERSION):
        raise ProtocolError(
            "unsupported_version",
            f"protocol version must be {PROTOCOL_VERSION}",
        )

    if payload.get("type") != "request":
        raise ProtocolError("invalid_type", "type must be 'request'")

    request_id = payload.get("id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ProtocolError("invalid_request", "id must be a non-empty string")
    if len(request_id) > MAX_REQUEST_ID_LENGTH:
        raise ProtocolError("invalid_request", "id is too long")
    if "\x00" in request_id:
        raise ProtocolError("invalid_request", "id contains a null byte")

    operation = payload.get("operation")
    if not isinstance(operation, str) or not operation:
        raise ProtocolError(
            "invalid_request", "operation must be a non-empty string",
            request_id=request_id,
        )
    if operation not in REQUEST_OPERATIONS:
        raise ProtocolError(
            "unsupported_operation",
            f"operation '{operation}' is not supported",
            request_id=request_id,
        )

    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProtocolError(
            "invalid_params", "params must be a JSON object",
            request_id=request_id,
        )

    if operation == "subscribe_events":
        try:
            _validate_subscription_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("audio_select_device", "audio.select_device"):
        try:
            _validate_audio_select_device_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("audio_start_capture", "audio.start_capture"):
        try:
            _validate_audio_start_capture_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("stt_update_config", "stt.update_config"):
        try:
            _validate_stt_update_config_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("stt_get_history", "stt.get_history"):
        try:
            _validate_stt_get_history_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("conversation_submit", "conversation.submit"):
        try:
            _validate_conversation_submit_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("conversation_update_config", "conversation.update_config"):
        try:
            _validate_conversation_update_config_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("conversation_get_history", "conversation.get_history"):
        try:
            _validate_stt_get_history_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif params:
        raise ProtocolError(
            "invalid_params",
            f"operation '{operation}' does not accept parameters",
            request_id=request_id,
        )

    return Request(request_id=request_id, operation=operation, params=params)


def _validate_audio_select_device_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"device"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    device = params.get("device")
    if device is not None and not isinstance(device, str):
        raise ProtocolError("invalid_params", "device must be a string")


def _validate_audio_start_capture_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"device"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    device = params.get("device")
    if device is not None and not isinstance(device, str):
        raise ProtocolError("invalid_params", "device must be a string")


def _validate_stt_update_config_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"language", "model"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    language = params.get("language")
    if language is not None and not isinstance(language, str):
        raise ProtocolError("invalid_params", "language must be a string")
    model = params.get("model")
    if model is not None and not isinstance(model, str):
        raise ProtocolError("invalid_params", "model must be a string")


def _validate_stt_get_history_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"limit"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    limit = params.get("limit")
    if limit is not None and not isinstance(limit, (int, float)):
        raise ProtocolError("invalid_params", "limit must be a number")


def _validate_conversation_submit_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"text"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    text = params.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ProtocolError("invalid_params", "text must be a non-empty string")


def _validate_conversation_update_config_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"cloud_enabled", "provider", "model", "omniroute_base_url", "api_key"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    cloud_enabled = params.get("cloud_enabled")
    if cloud_enabled is not None and not isinstance(cloud_enabled, bool):
        raise ProtocolError("invalid_params", "cloud_enabled must be a boolean")
    provider = params.get("provider")
    if provider is not None and not isinstance(provider, str):
        raise ProtocolError("invalid_params", "provider must be a string")
    model = params.get("model")
    if model is not None and not isinstance(model, str):
        raise ProtocolError("invalid_params", "model must be a string")
    base_url = params.get("omniroute_base_url")
    if base_url is not None and not isinstance(base_url, str):
        raise ProtocolError("invalid_params", "omniroute_base_url must be a string")
    api_key = params.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        raise ProtocolError("invalid_params", "api_key must be a string")


def _validate_subscription_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"events"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown subscription parameter(s): " + ", ".join(sorted(unknown)),
        )
    if "events" not in params:
        return
    events = params["events"]
    if not isinstance(events, list) or not events:
        raise ProtocolError("invalid_params", "events must be a non-empty list")
    if len(events) > MAX_PARAM_EVENTS:
        raise ProtocolError("invalid_params", "too many event filters")
    seen: set[str] = set()
    for name in events:
        if not isinstance(name, str) or name not in EVENT_NAME_SET:
            raise ProtocolError(
                "invalid_params",
                f"unknown event name: {name!r}",
            )
        if name in seen:
            raise ProtocolError("invalid_params", "duplicate event name")
        seen.add(name)


def encode_message(payload: dict[str, Any]) -> bytes:
    """Serialize one protocol message as a newline-terminated UTF-8 line."""
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def response(request_id: str | None, result: dict[str, Any]) -> bytes:
    return encode_message({
        "version": PROTOCOL_VERSION,
        "type": "response",
        "id": request_id,
        "ok": True,
        "result": result,
    })


def error_response(request_id: str | None, code: str, message: str) -> bytes:
    return encode_message({
        "version": PROTOCOL_VERSION,
        "type": "response",
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    })


def event_message(event: str, data: dict[str, Any], sequence: int) -> bytes:
    if event not in EVENT_NAME_SET:
        raise ValueError(f"unknown event: {event}")
    return encode_message({
        "version": PROTOCOL_VERSION,
        "type": "event",
        "event": event,
        "sequence": sequence,
        "data": data,
    })
