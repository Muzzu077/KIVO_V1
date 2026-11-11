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
import math
import re
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
    "command_resolve_approval",
    "command.resolve_approval",
    "command_get_pending_approvals",
    "command.get_pending_approvals",
    "media_get_status",
    "media.get_status",
    "media_play",
    "media.play",
    "media_pause",
    "media.pause",
    "media_play_pause",
    "media.play_pause",
    "media_next",
    "media.next",
    "media_previous",
    "media.previous",
    "media_seek",
    "media.seek",
    "media_get_lyrics",
    "media.get_lyrics",
    "telemetry_get_snapshot",
    "telemetry.get_snapshot",
    "mic_get_state",
    "mic.get_state",
    "mic_set_mute",
    "mic.set_mute",
    "clip_get_history",
    "clip.get_history",
    "clip_get_item",
    "clip.get_item",
    "clip_copy_item",
    "clip.copy_item",
    "clip_pin_item",
    "clip.pin_item",
    "clip_delete_item",
    "clip.delete_item",
    "clip_clear_history",
    "clip.clear_history",
    "shelf_list_items",
    "shelf.list_items",
    "shelf_add_uris",
    "shelf.add_uris",
    "shelf_remove_item",
    "shelf.remove_item",
    "shelf_clear",
    "shelf.clear",
    "shelf_open_item",
    "shelf.open_item",
    "bridge_start",
    "bridge.start",
    "bridge_stop",
    "bridge.stop",
    "bridge_get_status",
    "bridge.get_status",
    "bridge_approve_pairing",
    "bridge.approve_pairing",
    "bridge_reject_pairing",
    "bridge.reject_pairing",
    "bridge_revoke_device",
    "bridge.revoke_device",
    "bridge_get_transfers",
    "bridge.get_transfers",
    "bridge_resolve_transfer",
    "bridge.resolve_transfer",
    "bridge_cancel_transfer",
    "bridge.cancel_transfer",
    "bridge_create_download_ticket",
    "bridge.create_download_ticket",
    "bridge_get_downloads",
    "bridge.get_downloads",
    "bridge_cancel_download",
    "bridge.cancel_download",
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
    "command_approval_requested",
    "command.approval_requested",
    "command_approval_resolved",
    "command.approval_resolved",
    "media_status_changed",
    "media.status_changed",
    "media_lyrics_loaded",
    "media.lyrics_loaded",
    "telemetry_snapshot_changed",
    "telemetry.snapshot_changed",
    "mic_state_changed",
    "mic.state_changed",
    "clip_history_changed",
    "clip.history_changed",
    "shelf_items_changed",
    "shelf.items_changed",
    "bridge_status_changed",
    "bridge.status_changed",
    "bridge_pairing_requested",
    "bridge.pairing_requested",
    "bridge_transfer_requested",
    "bridge.transfer_requested",
    "bridge_transfer_progress",
    "bridge.transfer_progress",
    "bridge_transfer_completed",
    "bridge.transfer_completed",
    "bridge_download_requested",
    "bridge.download_requested",
    "bridge_download_progress",
    "bridge.download_progress",
    "bridge_download_completed",
    "bridge.download_completed",
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
    elif operation in ("command_resolve_approval", "command.resolve_approval"):
        try:
            _validate_command_resolve_approval_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("media_seek", "media.seek"):
        try:
            _validate_media_seek_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in (
        "media_play", "media.play",
        "media_pause", "media.pause",
        "media_play_pause", "media.play_pause",
        "media_get_status", "media.get_status",
        "media_get_lyrics", "media.get_lyrics",
    ):
        try:
            _validate_media_control_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("mic_set_mute", "mic.set_mute"):
        try:
            _validate_mic_set_mute_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("clip_get_history", "clip.get_history"):
        try:
            _validate_clip_get_history_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in (
        "clip_get_item", "clip.get_item",
        "clip_copy_item", "clip.copy_item",
        "clip_delete_item", "clip.delete_item",
    ):
        try:
            _validate_clip_item_id_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("clip_pin_item", "clip.pin_item"):
        try:
            _validate_clip_pin_item_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("shelf_add_uris", "shelf.add_uris"):
        try:
            _validate_shelf_add_uris_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in (
        "shelf_remove_item", "shelf.remove_item",
        "shelf_open_item", "shelf.open_item",
    ):
        try:
            _validate_shelf_item_id_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("bridge_start", "bridge.start"):
        try:
            _validate_bridge_start_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in (
        "bridge_approve_pairing", "bridge.approve_pairing",
        "bridge_reject_pairing", "bridge.reject_pairing",
        "bridge_revoke_device", "bridge.revoke_device",
    ):
        try:
            _validate_bridge_session_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("bridge_resolve_transfer", "bridge.resolve_transfer"):
        try:
            _validate_bridge_resolve_transfer_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("bridge_cancel_transfer", "bridge.cancel_transfer"):
        try:
            _validate_bridge_transfer_id_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in (
        "bridge_create_download_ticket", "bridge.create_download_ticket",
    ):
        try:
            _validate_bridge_create_download_params(params)
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code, exc.message, request_id=request_id
            ) from exc
    elif operation in ("bridge_cancel_download", "bridge.cancel_download"):
        try:
            _validate_bridge_download_id_params(params)
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


def _validate_command_resolve_approval_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"token", "approved", "turn_id", "tool_call_id"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    token = params.get("token")
    if not isinstance(token, str) or not token.strip():
        raise ProtocolError("invalid_params", "token must be a non-empty string")
    approved = params.get("approved")
    if not isinstance(approved, bool):
        raise ProtocolError("invalid_params", "approved must be a boolean")
    turn_id = params.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise ProtocolError(
            "invalid_params",
            "turn_id must be a non-empty string identifying the conversation turn",
        )
    if len(turn_id) > 128:
        raise ProtocolError("invalid_params", "turn_id is too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in turn_id):
        raise ProtocolError("invalid_params", "turn_id contains control characters")
    tool_call_id = params.get("tool_call_id")
    if tool_call_id is not None:
        if not isinstance(tool_call_id, str):
            raise ProtocolError(
                "invalid_params", "tool_call_id must be a string"
            )
        if len(tool_call_id) > 128:
            raise ProtocolError("invalid_params", "tool_call_id is too long")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in tool_call_id):
            raise ProtocolError(
                "invalid_params", "tool_call_id contains control characters"
            )


def _validate_media_seek_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"position_seconds", "player"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    pos = params.get("position_seconds")
    valid_position = False
    if isinstance(pos, (int, float)) and not isinstance(pos, bool):
        try:
            valid_position = math.isfinite(float(pos)) and pos >= 0
        except (OverflowError, ValueError):
            valid_position = False
    if not valid_position:
        raise ProtocolError(
            "invalid_params",
            "position_seconds must be a non-negative finite number",
        )
    player = params.get("player")
    if player is not None and not isinstance(player, str):
        raise ProtocolError("invalid_params", "player must be a string")


def _validate_media_control_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"player"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    player = params.get("player")
    if player is not None and not isinstance(player, str):
        raise ProtocolError("invalid_params", "player must be a string")


def _validate_mic_set_mute_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"muted"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    if "muted" not in params:
        raise ProtocolError("invalid_params", "missing required 'muted' parameter")
    muted = params.get("muted")
    if type(muted) is not bool:
        raise ProtocolError("invalid_params", "parameter 'muted' must be a boolean")


def _validate_clip_get_history_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"limit", "search", "filter"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    limit = params.get("limit")
    if limit is not None:
        if type(limit) is not int or limit < 1 or limit > 100:
            raise ProtocolError(
                "invalid_params", "limit must be an integer between 1 and 100"
            )
    search = params.get("search")
    if search is not None:
        if not isinstance(search, str):
            raise ProtocolError("invalid_params", "search must be a string")
        if len(search) > 128:
            raise ProtocolError(
                "invalid_params", "search query exceeds 128 characters"
            )
    ftype = params.get("filter")
    if ftype is not None:
        if ftype not in ("all", "text", "image", "pinned"):
            raise ProtocolError(
                "invalid_params",
                "filter must be 'all', 'text', 'image', or 'pinned'",
            )


def _validate_clip_item_id_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"id"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    if "id" not in params:
        raise ProtocolError("invalid_params", "missing required 'id' parameter")
    item_id = params.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ProtocolError("invalid_params", "id must be a non-empty string")
    if len(item_id) > 64:
        raise ProtocolError("invalid_params", "id is too long")
    if not re.match(r"^[A-Za-z0-9_.-]+$", item_id):
        raise ProtocolError("invalid_params", "id contains invalid characters")


def _validate_clip_pin_item_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"id", "pin"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    if "id" not in params:
        raise ProtocolError("invalid_params", "missing required 'id' parameter")
    item_id = params.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ProtocolError("invalid_params", "id must be a non-empty string")
    if len(item_id) > 64:
        raise ProtocolError("invalid_params", "id is too long")
    if not re.match(r"^[A-Za-z0-9_.-]+$", item_id):
        raise ProtocolError("invalid_params", "id contains invalid characters")
    pin = params.get("pin")
    if pin is not None and type(pin) is not bool:
        raise ProtocolError("invalid_params", "pin must be a boolean")


def _validate_shelf_add_uris_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"uris"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    if "uris" not in params:
        raise ProtocolError("invalid_params", "missing required 'uris' parameter")
    uris = params.get("uris")
    if not isinstance(uris, list):
        raise ProtocolError("invalid_params", "uris must be a list of URI strings")
    if not uris:
        raise ProtocolError("invalid_params", "uris list must not be empty")
    if len(uris) > 20:
        raise ProtocolError("invalid_params", "uris list exceeds maximum batch size of 20")
    for u in uris:
        if not isinstance(u, str):
            raise ProtocolError("invalid_params", "all entries in uris must be strings")
        if len(u.encode("utf-8")) > 4096:
            raise ProtocolError("invalid_params", "URI exceeds maximum limit of 4096 bytes")


def _validate_shelf_item_id_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"id"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    if "id" not in params:
        raise ProtocolError("invalid_params", "missing required 'id' parameter")
    item_id = params.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ProtocolError("invalid_params", "id must be a non-empty string")
    if len(item_id) > 64:
        raise ProtocolError("invalid_params", "id is too long")
    if not re.match(r"^[A-Za-z0-9_.-]+$", item_id):
        raise ProtocolError("invalid_params", "id contains invalid characters")


def _validate_bridge_start_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"port"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    port = params.get("port")
    if port is not None:
        if type(port) is not int or port < 1024 or port > 65535:
            raise ProtocolError(
                "invalid_params", "port must be an integer between 1024 and 65535"
            )


def _validate_bridge_session_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"session_id", "device_id"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    sess_id = params.get("session_id") or params.get("device_id")
    if not isinstance(sess_id, str) or not sess_id.strip():
        raise ProtocolError("invalid_params", "missing required 'session_id' parameter")
    if len(sess_id) > 64:
        raise ProtocolError("invalid_params", "session_id is too long")
    if not re.match(r"^[A-Za-z0-9_.-]+$", sess_id):
        raise ProtocolError("invalid_params", "session_id contains invalid characters")


def _validate_bridge_transfer_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"tr_[0-9a-f]{24}", value):
        raise ProtocolError("invalid_params", "transfer_id is malformed")
    return value


def _validate_bridge_transfer_id_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"transfer_id"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    if "transfer_id" not in params:
        raise ProtocolError("invalid_params", "missing required 'transfer_id' parameter")
    _validate_bridge_transfer_id(params.get("transfer_id"))


def _validate_bridge_resolve_transfer_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"transfer_id", "decision"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    if "transfer_id" not in params:
        raise ProtocolError("invalid_params", "missing required 'transfer_id' parameter")
    _validate_bridge_transfer_id(params.get("transfer_id"))
    decision = params.get("decision")
    if decision not in ("allow_receive", "reject_receive", "accept_file", "discard_file"):
        raise ProtocolError(
            "invalid_params",
            "decision must be one of allow_receive, reject_receive, accept_file, discard_file",
        )


def _validate_bridge_create_download_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"item_id", "authorize"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    item_id = params.get("item_id")
    if not isinstance(item_id, str) or not re.fullmatch(r"shelf_[0-9a-f]{12}", item_id):
        raise ProtocolError("invalid_params", "item_id is malformed")
    if params.get("authorize") is not True:
        raise ProtocolError(
            "invalid_params",
            "explicit export authorization requires authorize=true",
        )


def _validate_bridge_download_id_params(params: dict[str, Any]) -> None:
    unknown = set(params) - {"download_id"}
    if unknown:
        raise ProtocolError(
            "invalid_params",
            "unknown parameter(s): " + ", ".join(sorted(unknown)),
        )
    if "download_id" not in params:
        raise ProtocolError("invalid_params", "missing required 'download_id' parameter")
    download_id = params.get("download_id")
    if not isinstance(download_id, str) or not re.fullmatch(r"tkt_[0-9a-f]{64}", download_id):
        raise ProtocolError("invalid_params", "download_id is malformed")


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
