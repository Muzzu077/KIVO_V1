"""
core/drive.py — one place that knows whether KIVO is driving the desktop.

WHY THIS EXISTS
    "Stop" was three unrelated things. The voice interrupt drained the speaker
    queue. A running orchestrate_task kept going, because nothing told it to
    stop. The HUD had no way to show that a multi-step job was in progress, so
    the only evidence of desktop control was windows moving on their own.

    A DriveSession is the shared state behind all three: it is active while an
    agent-level job is touching the desktop, it carries the current step for the
    overlay, and stop() is what both the voice path and the on-screen button
    call. It does not perform actions and it does not own a thread.

    Talk never starts a session. A single answer, a search or a question leaves
    it inactive; the overlay appearing is therefore a truthful signal that
    something sustained is happening.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class DriveState:
    active: bool = False
    goal: str = ""
    action: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    reason: str = ""

    def snapshot(self) -> dict:
        return {
            "active": self.active,
            "goal": self.goal,
            "action": self.action,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "elapsed": (time.monotonic() - self.started_at) if self.active else 0.0,
            "reason": self.reason,
        }


class DriveSession:
    """Thread-safe state plus listeners. No UI imports, no action execution."""

    _instance: Optional["DriveSession"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._lock = threading.Lock()
        self._state = DriveState()
        self._listeners: list[Callable[[dict], None]] = []

    @classmethod
    def get_instance(cls) -> "DriveSession":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── Listeners ──────────────────────────────────────────────────────────

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _notify(self, state: dict) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(state)
            except Exception as e:
                print(f"[Drive] listener failed: {e}")

    # ── State ──────────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._state.active

    def snapshot(self) -> dict:
        with self._lock:
            return self._state.snapshot()

    def start(self, goal: str) -> dict:
        """Begin a sustained job. Idempotent: an active goal is not replaced."""
        with self._lock:
            if self._state.active:
                return self._state.snapshot()
            now = time.monotonic()
            self._state = DriveState(
                active=True,
                goal=(goal or "Working").strip()[:160],
                action="Starting…",
                started_at=now,
                updated_at=now,
            )
            snapshot = self._state.snapshot()
        print(f"[Drive] ▶️ {snapshot['goal']}")
        self._notify(snapshot)
        return snapshot

    def update(self, action: str, goal: str = "") -> dict:
        """Show the current step. Ignored when no session is active."""
        with self._lock:
            if not self._state.active:
                return self._state.snapshot()
            self._state.action = (action or "").strip()[:200]
            if goal:
                self._state.goal = goal.strip()[:160]
            self._state.updated_at = time.monotonic()
            snapshot = self._state.snapshot()
        self._notify(snapshot)
        return snapshot

    def stop(self, reason: str = "stopped") -> dict:
        with self._lock:
            was_active = self._state.active
            self._state.active = False
            self._state.reason = reason
            self._state.updated_at = time.monotonic()
            snapshot = self._state.snapshot()
        if was_active:
            print(f"[Drive] ⏹️ {reason}")
            self._notify(snapshot)
        return snapshot


def get_drive_session() -> DriveSession:
    return DriveSession.get_instance()
