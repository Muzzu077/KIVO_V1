"""Thread-safe runtime state owned by the KIVO application.

The IPC event stream must reflect real state, not synthetic UI data.  This
store is the single place where assistant, audio and agent state live.  Audio
is deliberately ``unavailable`` until an audio pipeline is actually bound in a
later milestone; no listener or microphone is started here.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from kivo.core.state import KivoStateType

StateListener = Callable[[str, dict], None]

ASSISTANT_STATES = frozenset({
    "idle", "listening", "thinking", "speaking", "driving", "error",
    "wake_word", "voice_command", "cancellation", "full_screen_mode",
    "settings_mode", "dashboard_open", "mute",
})
AUDIO_STATES = frozenset({
    "unavailable", "idle", "listening", "capturing", "speaking", "muted", "error",
})
AGENT_STATES = frozenset({
    "idle", "planning", "working", "waiting", "error",
})


class RuntimeState:
    """Small observable state store for IPC-facing backend status."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._assistant_state = "idle"
        self._audio_state = "unavailable"
        self._agent_state = "idle"
        self._assistant_detail = "backend started"
        self._audio_detail = "audio pipeline is not initialized"
        self._agent_detail = "no active task"
        self._listeners: list[StateListener] = []

    # ── Listeners ──────────────────────────────────────────────────────────

    def add_listener(self, callback: StateListener) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: StateListener) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _notify(self, event: str, data: dict) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(event, data)
            except Exception:
                pass

    # ── Snapshots ──────────────────────────────────────────────────────────

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started_at)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "assistant_state": self._assistant_state,
                "audio_state": self._audio_state,
                "agent_state": self._agent_state,
                "assistant_detail": self._assistant_detail,
                "audio_detail": self._audio_detail,
                "agent_detail": self._agent_detail,
                "uptime_seconds": round(max(0.0, time.monotonic() - self._started_at), 3),
            }

    # ── Mutators ───────────────────────────────────────────────────────────

    def set_assistant_state(self, state: KivoStateType | str, detail: str = "") -> bool:
        value = self._coerce(state, ASSISTANT_STATES, "assistant")
        with self._lock:
            previous = self._assistant_state
            if previous == value and (not detail or detail == self._assistant_detail):
                return False
            self._assistant_state = value
            self._assistant_detail = str(detail or "")
            data = {"state": value, "previous": previous, "detail": self._assistant_detail}
        self._notify("assistant_state_changed", data)
        return True

    def set_audio_state(self, state: str, detail: str = "") -> bool:
        value = self._coerce(state, AUDIO_STATES, "audio")
        with self._lock:
            previous = self._audio_state
            if previous == value and (not detail or detail == self._audio_detail):
                return False
            self._audio_state = value
            self._audio_detail = str(detail or "")
            data = {"state": value, "previous": previous, "detail": self._audio_detail}
        self._notify("audio_state_changed", data)
        return True

    def set_agent_state(self, state: str, detail: str = "") -> bool:
        value = self._coerce(state, AGENT_STATES, "agent")
        with self._lock:
            previous = self._agent_state
            if previous == value and (not detail or detail == self._agent_detail):
                return False
            self._agent_state = value
            self._agent_detail = str(detail or "")
            data = {"state": value, "previous": previous, "detail": self._agent_detail}
        self._notify("agent_state_changed", data)
        return True

    @staticmethod
    def _coerce(state: KivoStateType | str, allowed: frozenset[str], label: str) -> str:
        if isinstance(state, KivoStateType):
            value = state.name.lower()
        else:
            value = str(state or "").strip().lower()
        if value not in allowed:
            raise ValueError(f"unknown {label} state: {state!r}")
        return value
