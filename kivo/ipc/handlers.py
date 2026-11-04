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
    ):
        self._state = state
        self._drive = drive
        self._orchestrator = orchestrator
        self._audio_dm = audio_device_manager
        self._audio_cm = audio_capture_manager
        self._audio_ingress = audio_ingress
        self._stt = transcription_manager
        self._conv = conversation_manager

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
        if "cloud_enabled" in params:
            self._conv.set_cloud_enabled(bool(params["cloud_enabled"]))
        if "model" in params and isinstance(params["model"], str) and params["model"].strip():
            self._conv.set_model(params["model"].strip())
        return {"ok": True, "status": self.conversation_get_status()}

    def _drive_snapshot(self) -> dict:
        state = self._drive.snapshot()
        return {
            "active": bool(state.get("active")),
            "goal": str(state.get("goal", "")),
            "action": str(state.get("action", "")),
            "reason": str(state.get("reason", "")),
            "elapsed": round(float(state.get("elapsed", 0.0) or 0.0), 3),
        }
