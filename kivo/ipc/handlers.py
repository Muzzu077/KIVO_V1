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

    def __init__(self, state: RuntimeState, drive, orchestrator):
        self._state = state
        self._drive = drive
        self._orchestrator = orchestrator

    def get_status(self) -> dict:
        snapshot = self._state.snapshot()
        return {
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

    def get_current_task(self) -> dict:
        plan = self._orchestrator.get_current_task()
        if plan is None:
            return {"task": None}
        return {"task": self._orchestrator.public_task_snapshot(plan)}

    def get_dashboard_data(self) -> dict:
        return {
            "status": self.get_status(),
            "current_task": self.get_current_task()["task"],
            "task_history": self._orchestrator.task_history_snapshot(limit=10),
        }

    def cancel_active_task(self) -> dict:
        was_active = self._orchestrator.has_active_tasks()
        message = self._orchestrator.cancel_active()
        return {
            "cancelled": was_active,
            "message": message,
        }

    def _drive_snapshot(self) -> dict:
        state = self._drive.snapshot()
        return {
            "active": bool(state.get("active")),
            "goal": str(state.get("goal", "")),
            "action": str(state.get("action", "")),
            "reason": str(state.get("reason", "")),
            "elapsed": round(float(state.get("elapsed", 0.0) or 0.0), 3),
        }
