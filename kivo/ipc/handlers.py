"""Validated frontend operations resolved against live KIVO backend services."""
from __future__ import annotations

import os

from kivo import __version__
from kivo.ipc.protocol import PROTOCOL_VERSION
from kivo.runtime_state import RuntimeState


class IPCHandlers:
    """The only operations the frontend can invoke in this stage.

    Deliberately absent: shell execution, filesystem writes, memory dumps,
    API-key access, voiceprint access and confirmation resolution.
    """

    def __init__(
        self,
        state: RuntimeState,
        drive,
        orchestrator,
        audio_device_manager=None,
        audio_capture_manager=None,
        audio_ingress=None,
        transcription_manager=None,
        conversation_manager=None,
        media_service=None,
        telemetry_service=None,
        microphone_service=None,
        clipboard_service=None,
        shelf_service=None,
        bridge_service=None,
    ):
        self._state = state
        self._drive = drive
        self._orchestrator = orchestrator
        self._audio_dm = audio_device_manager
        self._audio_cm = audio_capture_manager
        self._audio_ingress = audio_ingress
        self._stt = transcription_manager
        self._conv = conversation_manager
        self._media = media_service
        self._telemetry = telemetry_service
        self._microphone = microphone_service
        self._clipboard = clipboard_service
        self._shelf = shelf_service
        self._bridge = bridge_service

    def get_status(self) -> dict:
        snapshot = self._state.snapshot()
        status_dict = {
            "service": "kivo",
            "version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "assistant_state": snapshot["assistant_state"],
            "assistant_detail": snapshot["assistant_detail"],
            "audio_state": snapshot["audio_state"],
            "audio_detail": snapshot["audio_detail"],
            "agent_state": snapshot["agent_state"],
            "agent_detail": snapshot["agent_detail"],
            "uptime_seconds": snapshot["uptime_seconds"],
            "active_task": self._orchestrator.has_active_tasks(),
            "drive": self._drive_snapshot(),
        }
        if self._audio_dm is not None:
            status_dict["audio"] = self.audio_get_status()
        if self._stt is not None:
            status_dict["stt"] = self.stt_get_status()
        if self._conv is not None:
            status_dict["conversation"] = self.conversation_get_status()
        if self._media is not None:
            status_dict["media"] = self.media_get_status()
        return status_dict

    def get_current_task(self) -> dict:
        plan = self._orchestrator.get_current_task()
        if plan is None:
            return {"task": None}
        return {"task": self._orchestrator.public_task_snapshot(plan)}

    def get_dashboard_data(self) -> dict:
        data = {
            "status": self.get_status(),
            "current_task": self.get_current_task()["task"],
            "task_history": self._orchestrator.task_history_snapshot(limit=10),
        }
        if self._audio_dm is not None:
            data["audio"] = self.audio_get_status()
        if self._stt is not None:
            data["stt"] = self.stt_get_status()
            data["transcriptions"] = self._stt.get_history(limit=10)
        if self._conv is not None:
            data["conversation"] = self.conversation_get_status()
            data["conversation_history"] = self.conversation_get_history({"limit": 20}).get("history", [])
        if self._media is not None:
            data["media"] = self.media_get_status()
        return data

    def cancel_active_task(self) -> dict:
        was_active = self._orchestrator.has_active_tasks()
        message = self._orchestrator.cancel_active()
        return {
            "cancelled": was_active,
            "message": message,
        }

    # ── Audio operations ───────────────────────────────────────────────────

    def audio_get_status(self) -> dict:
        if self._audio_dm is None or self._audio_cm is None:
            return {
                "available": False,
                "state": "unavailable",
                "device": None,
                "devices_count": 0,
                "capturing": False,
                "duration": 0.0,
                "peak_amplitude": 0.0,
                "rms_amplitude": 0.0,
                "error_message": "Audio subsystem is not initialized",
                "last_capture": None,
            }
        devices = self._audio_dm.list_devices()
        selected = self._audio_dm.get_selected_device()
        cap_status = self._audio_cm.get_status()
        last_meta = self._audio_ingress.get_latest_metadata() if self._audio_ingress else None
        return {
            "available": len(devices) > 0 and (selected.is_available if selected else False),
            "state": cap_status.state,
            "device": selected.to_dict() if selected else None,
            "devices_count": len(devices),
            "capturing": cap_status.state == "capturing",
            "duration": cap_status.duration,
            "peak_amplitude": cap_status.peak_amplitude,
            "rms_amplitude": cap_status.rms_amplitude,
            "error_message": cap_status.error_message,
            "last_capture": last_meta,
        }

    def audio_list_devices(self) -> dict:
        if self._audio_dm is None:
            return {"devices": [], "selected": None}
        devices = self._audio_dm.list_devices(refresh=True)
        selected = self._audio_dm.get_selected_device()
        return {
            "devices": [d.to_dict() for d in devices],
            "selected": selected.to_dict() if selected else None,
        }

    def audio_select_device(self, params: dict | None = None) -> dict:
        if self._audio_dm is None:
            return {"ok": False, "error": "Audio subsystem not initialized"}
        target = ""
        if params and isinstance(params, dict):
            target = params.get("device", "") or ""
        try:
            device = self._audio_dm.select_device(target)
            return {"ok": True, "device": device.to_dict()}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def audio_start_capture(self, params: dict | None = None) -> dict:
        if self._audio_cm is None:
            return {"ok": False, "error": "Audio subsystem not initialized"}
        device = params.get("device") if (params and isinstance(params, dict)) else None
        ok = self._audio_cm.start_capture(device)
        status = self._audio_cm.get_status()
        return {
            "ok": ok,
            "state": status.state,
            "device_name": status.device_name,
            "error": status.error_message if not ok else None,
        }

    def audio_stop_capture(self) -> dict:
        if self._audio_cm is None:
            return {"ok": False, "capture": None}
        capture = self._audio_cm.stop_capture()
        if capture is None:
            return {"ok": False, "capture": None}
        return {
            "ok": True,
            "capture": capture.metadata(),
        }

    def audio_cancel_capture(self) -> dict:
        if self._audio_cm is None:
            return {"ok": False}
        ok = self._audio_cm.cancel_capture()
        return {"ok": ok}

    # ── STT operations ─────────────────────────────────────────────────────

    def stt_get_status(self) -> dict:
        if self._stt is None:
            return {
                "available": False,
                "state": "unavailable",
                "engine": "none",
                "model": "none",
                "language": "auto",
                "queue_size": 0,
                "last_result": None,
                "error_message": "STT subsystem is not initialized",
            }
        return self._stt.get_status().to_dict()

    def stt_get_history(self, params: dict | None = None) -> dict:
        if self._stt is None:
            return {"history": []}
        limit = 10
        if params and isinstance(params, dict):
            try:
                limit = max(1, min(30, int(params.get("limit", 10))))
            except (ValueError, TypeError):
                limit = 10
        return {"history": self._stt.get_history(limit=limit)}

    def stt_get_result(self, params: dict | None = None) -> dict:
        if self._stt is None:
            return {"result": None}
        latest = self._stt.get_latest_result()
        return {"result": latest.to_dict() if latest else None}

    def stt_update_config(self, params: dict | None = None) -> dict:
        if self._stt is None:
            return {"ok": False, "error": "STT subsystem is not initialized"}
        if not params or not isinstance(params, dict):
            return {"ok": False, "error": "Missing parameters"}
        lang = params.get("language")
        if lang is not None:
            self._stt.set_language(str(lang))
        return {"ok": True, "status": self.stt_get_status()}

    def stt_cancel(self) -> dict:
        # In single-shot PTT, cancellation stops any queued audio
        if self._stt is None:
            return {"ok": False}
        return {"ok": True}

    # ── Conversation operations ────────────────────────────────────────────

    def conversation_get_status(self) -> dict:
        if self._conv is None:
            return {
                "state": "unavailable",
                "active_conversation_id": "",
                "message_count": 0,
                "cloud_enabled": False,
                "provider": {"provider_name": "none", "model": "", "cloud_enabled": False, "available": False},
                "last_response_preview": "",
                "error_message": "Conversation subsystem is not initialized",
            }
        return self._conv.get_status().to_dict()

    def conversation_get_history(self, params: dict | None = None) -> dict:
        if self._conv is None:
            return {"history": []}
        return {"history": self._conv.get_history()}

    def conversation_submit(self, params: dict | None = None) -> dict:
        if self._conv is None:
            return {"ok": False, "error": "Conversation subsystem is not initialized"}
        if not params or not isinstance(params, dict):
            return {"ok": False, "error": "Missing parameters"}
        text = str(params.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "Text cannot be empty"}
        submitted = self._conv.submit_text(text)
        return {"ok": submitted, "status": self.conversation_get_status()}

    def conversation_cancel(self) -> dict:
        if self._conv is None:
            return {"ok": False}
        cancelled = self._conv.cancel_active()
        return {"ok": cancelled}

    def conversation_clear(self) -> dict:
        if self._conv is None:
            return {"ok": False}
        self._conv.clear()
        return {"ok": True}

    def conversation_update_config(self, params: dict | None = None) -> dict:
        if self._conv is None:
            return {"ok": False, "error": "Conversation subsystem is not initialized"}
        if not params or not isinstance(params, dict):
            return {"ok": False, "error": "Missing parameters"}

        # Handle secret API key storage (0600 mode)
        if "api_key" in params and isinstance(params["api_key"], str):
            from kivo.conversation.conversation_config import save_omniroute_api_key
            save_omniroute_api_key(params["api_key"])

        # Handle provider switch / URL / model
        new_provider = params.get("provider")
        new_url = params.get("omniroute_base_url")
        new_model = params.get("model")

        if new_provider:
            try:
                self._conv.set_provider(new_provider, base_url=new_url, model=new_model)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        else:
            if new_url and hasattr(self._conv, "set_omniroute_base_url"):
                self._conv.set_omniroute_base_url(new_url)
            if new_model and hasattr(self._conv, "set_model"):
                self._conv.set_model(new_model)

        if "cloud_enabled" in params:
            self._conv.set_cloud_enabled(bool(params["cloud_enabled"]))

        return {"ok": True, "status": self.conversation_get_status()}

    def conversation_list_models(self) -> dict:
        if self._conv is None:
            return {"ok": False, "models": [], "error": "Conversation subsystem not initialized"}
        try:
            models = self._conv.list_models()
            return {"ok": True, "models": models}
        except Exception as exc:
            return {"ok": False, "models": [], "error": str(exc)}

    def conversation_test_connection(self) -> dict:
        if self._conv is None:
            return {"ok": False, "message": "Conversation subsystem not initialized"}
        try:
            ok, msg = self._conv.test_connection()
            return {"ok": ok, "message": msg}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    # ── Command execution & approval operations ────────────────────────────

    def command_resolve_approval(self, params: dict | None = None) -> dict:
        if self._conv is None:
            return {"ok": False, "message": "Conversation subsystem not initialized"}
        if not params or not isinstance(params, dict):
            return {"ok": False, "message": "Missing parameters"}
        token = str(params.get("token", "")).strip()
        raw_approved = params.get("approved")
        if not token or not isinstance(raw_approved, bool):
            return {"ok": False, "message": "token and approved are required"}
        raw_turn = params.get("turn_id")
        turn_id = raw_turn.strip() if isinstance(raw_turn, str) else ""
        if not turn_id:
            return {
                "ok": False,
                "message": (
                    "A conversation turn_id is required to resolve an approval."
                ),
            }
        raw_call = params.get("tool_call_id")
        tool_call_id = raw_call.strip() if isinstance(raw_call, str) else ""
        ok, msg = self._conv.resolve_command_approval(
            token,
            raw_approved,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )
        return {"ok": ok, "message": msg}

    def command_get_pending_approvals(self) -> dict:
        if self._conv is None:
            return {"ok": True, "pending": []}
        pending = self._conv.get_pending_approvals()
        return {"ok": True, "pending": pending}

    # ── MPRIS media operations ─────────────────────────────────────────────

    def media_get_status(self, params: dict | None = None) -> dict:
        if self._media is None:
            return {
                "available": False,
                "player_name": "",
                "players": [],
                "status": "Unavailable",
                "title": "",
                "artist": "",
                "album": "",
                "art_url": "",
                "position_seconds": 0.0,
                "duration_seconds": 0.0,
                "can_seek": False,
                "can_play_pause": False,
                "can_go_next": False,
                "can_go_previous": False,
                "lyrics_available": False,
            }
        return self._media.get_status_dict()

    def media_play(self, params: dict | None = None) -> dict:
        if self._media is None:
            return {"ok": False, "error": "Media subsystem is not initialized"}
        return self._media.play_sync()

    def media_pause(self, params: dict | None = None) -> dict:
        if self._media is None:
            return {"ok": False, "error": "Media subsystem is not initialized"}
        return self._media.pause_sync()

    def media_play_pause(self, params: dict | None = None) -> dict:
        if self._media is None:
            return {"ok": False, "error": "Media subsystem is not initialized"}
        return self._media.play_pause_sync()

    def media_next(self, params: dict | None = None) -> dict:
        if self._media is None:
            return {"ok": False, "error": "Media subsystem is not initialized"}
        return self._media.next_sync()

    def media_previous(self, params: dict | None = None) -> dict:
        if self._media is None:
            return {"ok": False, "error": "Media subsystem is not initialized"}
        return self._media.previous_sync()

    def media_seek(self, params: dict | None = None) -> dict:
        if self._media is None:
            return {"ok": False, "error": "Media subsystem is not initialized"}
        if not params or not isinstance(params, dict):
            return {"ok": False, "error": "Missing parameters"}
        pos = params.get("position_seconds")
        try:
            return self._media.seek_sync(float(pos))
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}

    def media_get_lyrics(self, params: dict | None = None) -> dict:
        if self._media is None:
            return {"track": "", "artist": "", "synced": False, "lines": []}
        return self._media.get_lyrics_dict()

    # ── Telemetry operations ───────────────────────────────────────────────

    def telemetry_get_snapshot(self, params: dict | None = None) -> dict:
        if self._telemetry is None:
            from kivo.telemetry.models import TelemetrySnapshot
            return TelemetrySnapshot.unavailable(reason="service_unavailable").to_dict()
        return self._telemetry.latest_snapshot().to_dict()

    # ── Microphone operations ──────────────────────────────────────────────

    def mic_get_state(self, params: dict | None = None) -> dict:
        if self._microphone is None:
            from kivo.microphone.models import MicState
            return MicState.unavailable(
                error_code="service_unavailable",
                error_message="Microphone service is not initialized",
            ).to_dict()
        return self._microphone.get_state().to_dict()

    def mic_set_mute(self, params: dict | None = None) -> dict:
        if self._microphone is None:
            from kivo.microphone.models import MicResult, MicState
            st = MicState.unavailable(
                error_code="service_unavailable",
                error_message="Microphone service is not initialized",
            )
            return MicResult(
                ok=False,
                requested_muted=bool(params.get("muted") if isinstance(params, dict) else False),
                changed=False,
                default_source_changed=False,
                duration_ms=0,
                state=st,
                error_code="service_unavailable",
                error_message="Microphone service is not initialized",
            ).to_dict()
        muted = params["muted"] if isinstance(params, dict) and "muted" in params else False
        return self._microphone.set_mute(muted).to_dict()

    # ── Clipboard operations ───────────────────────────────────────────────

    def clip_get_history(self, params: dict | None = None) -> dict:
        if self._clipboard is None:
            return {"items": [], "total_count": 0}
        limit = int(params.get("limit", 50)) if params else 50
        search = str(params.get("search", "")) if params else ""
        ftype = str(params.get("filter", "all")) if params else "all"
        items = self._clipboard.get_history(limit=limit, search=search, filter_type=ftype)
        return {"items": [it.to_dict() for it in items], "total_count": len(items)}

    def clip_get_item(self, params: dict | None = None) -> dict:
        if self._clipboard is None or not params or "id" not in params:
            return {"ok": False, "item": None}
        detail = self._clipboard.get_item(params["id"])
        return {"ok": detail is not None, "item": detail.to_dict() if detail else None}

    def clip_copy_item(self, params: dict | None = None) -> dict:
        if self._clipboard is None or not params or "id" not in params:
            return {"ok": False}
        ok = self._clipboard.copy_item(params["id"])
        return {"ok": ok}

    def clip_pin_item(self, params: dict | None = None) -> dict:
        if self._clipboard is None or not params or "id" not in params:
            return {"ok": False}
        pin = bool(params.get("pin", True))
        try:
            ok = self._clipboard.pin_item(params["id"], pin=pin)
            return {"ok": ok}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def clip_delete_item(self, params: dict | None = None) -> dict:
        if self._clipboard is None or not params or "id" not in params:
            return {"ok": False}
        ok = self._clipboard.delete_item(params["id"])
        return {"ok": ok}

    def clip_clear_history(self, params: dict | None = None) -> dict:
        if self._clipboard is None:
            return {"ok": False}
        ok = self._clipboard.clear_history()
        return {"ok": ok}

    # ── File Shelf operations ──────────────────────────────────────────────

    def shelf_list_items(self, params: dict | None = None) -> dict:
        if self._shelf is None:
            return {"items": []}
        return {"items": [it.to_dict() for it in self._shelf.list_items()]}

    def shelf_add_uris(self, params: dict | None = None) -> dict:
        if self._shelf is None or not params or "uris" not in params:
            return {"added": [], "rejected": []}
        added, rejected = self._shelf.add_items(params["uris"])
        return {
            "added": [it.to_dict() for it in added],
            "rejected": rejected,
        }

    def shelf_remove_item(self, params: dict | None = None) -> dict:
        if self._shelf is None or not params or "id" not in params:
            return {"ok": False}
        ok = self._shelf.remove_item(params["id"])
        return {"ok": ok}

    def shelf_clear(self, params: dict | None = None) -> dict:
        if self._shelf is None:
            return {"ok": False}
        ok = self._shelf.clear()
        return {"ok": ok}

    def shelf_open_item(self, params: dict | None = None) -> dict:
        if self._shelf is None or not params or "id" not in params:
            return {
                "ok": False,
                "error_code": "service_unavailable",
                "error_message": "File Shelf service is unavailable",
            }
        ok, code, msg = self._shelf.open_item(params["id"])
        return {"ok": ok, "error_code": code, "error_message": msg}

    # ── Phone Bridge operations ────────────────────────────────────────────

    def bridge_start(self, params: dict | None = None) -> dict:
        if self._bridge is None:
            return {"ok": False, "error": "Phone Bridge service is unavailable"}
        port = int(params.get("port", 8443)) if params and "port" in params else 8443
        return self._bridge.start_bridge(preferred_port=port)

    def bridge_stop(self, params: dict | None = None) -> dict:
        if self._bridge is None:
            return {"ok": False}
        ok = self._bridge.stop_bridge()
        return {"ok": ok}

    def bridge_get_status(self, params: dict | None = None) -> dict:
        if self._bridge is None:
            from kivo.bridge.models import BridgeStatus
            return BridgeStatus().to_dict()
        return self._bridge.get_status().to_dict()

    def bridge_approve_pairing(self, params: dict | None = None) -> dict:
        if self._bridge is None or not params:
            return {"ok": False}
        sess_id = str(params.get("session_id") or params.get("device_id") or "")
        if not sess_id:
            return {"ok": False}
        ok = self._bridge.approve_pairing(sess_id)
        return {"ok": ok}

    def bridge_reject_pairing(self, params: dict | None = None) -> dict:
        if self._bridge is None or not params:
            return {"ok": False}
        sess_id = str(params.get("session_id") or params.get("device_id") or "")
        if not sess_id:
            return {"ok": False}
        ok = self._bridge.reject_pairing(sess_id)
        return {"ok": ok}

    def bridge_revoke_device(self, params: dict | None = None) -> dict:
        if self._bridge is None or not params:
            return {"ok": False}
        sess_id = str(params.get("session_id") or params.get("device_id") or "")
        if not sess_id:
            return {"ok": False}
        ok = self._bridge.revoke_device(sess_id)
        return {"ok": ok}

    def bridge_get_transfers(self, params: dict | None = None) -> dict:
        if self._bridge is None:
            return {"ok": False, "transfers": []}
        return self._bridge.get_transfers()

    def bridge_resolve_transfer(self, params: dict | None = None) -> dict:
        if self._bridge is None or not params:
            return {"ok": False, "error_code": "service_unavailable"}
        transfer_id = params.get("transfer_id")
        decision = params.get("decision")
        if not isinstance(transfer_id, str) or not isinstance(decision, str):
            return {"ok": False, "error_code": "invalid_params"}
        return self._bridge.resolve_transfer(transfer_id, decision)

    def bridge_cancel_transfer(self, params: dict | None = None) -> dict:
        if self._bridge is None or not params:
            return {"ok": False, "error_code": "service_unavailable"}
        transfer_id = params.get("transfer_id")
        if not isinstance(transfer_id, str):
            return {"ok": False, "error_code": "invalid_params"}
        return self._bridge.cancel_transfer(transfer_id)

    def bridge_create_download_ticket(self, params: dict | None = None) -> dict:
        if self._bridge is None:
            return {"ok": False, "error_code": "service_unavailable"}
        if not params:
            return {"ok": False, "error_code": "invalid_params"}
        return self._bridge.create_download_ticket(
            params.get("item_id"), params.get("authorize")
        )

    def bridge_get_downloads(self, params: dict | None = None) -> dict:
        if self._bridge is None:
            return {"ok": False, "downloads": [], "error_code": "service_unavailable"}
        return self._bridge.get_downloads()

    def bridge_cancel_download(self, params: dict | None = None) -> dict:
        if self._bridge is None or not params:
            return {"ok": False, "error_code": "service_unavailable"}
        download_id = params.get("download_id")
        if not isinstance(download_id, str):
            return {"ok": False, "error_code": "invalid_params"}
        return self._bridge.cancel_download(download_id)

    def _drive_snapshot(self) -> dict:
        state = self._drive.snapshot()
        return {
            "active": bool(state.get("active")),
            "goal": str(state.get("goal", "")),
            "action": str(state.get("action", "")),
            "reason": str(state.get("reason", "")),
            "elapsed": round(float(state.get("elapsed", 0.0) or 0.0), 3),
        }
