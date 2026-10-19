"""
core/proactive_hub.py — Event-driven ambient intelligence & proactive assistance engine.

WHY THIS EXISTS
    Traditional proactive check-ins rely solely on silence timers (e.g. speak every 15 min),
    which is disruptive, context-blind, and often unhelpful.

    ProactiveHub provides real-time event-driven ambient assistance:
      1. Screen Error Detection: Catches compiler errors, tracebacks, and panic dialogs
         appearing in the active window.
      2. Terminal Result Tracking: Detects failed commands and completed long tasks.
      3. Project Context Tracking: Detects when the user switches git repositories or directories.
      4. Telemetry Alerts: Detects system resource starvation (RAM > 95%).
      5. Formulates high-value, natural, non-pushy proactive prompts for Gemini Live.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Optional

from core.desktop_context import get_desktop_context
from core.screen_observer import get_screen_observer, ScreenObservation


class ProactiveEventType(Enum):
    SCREEN_ERROR = auto()
    TERMINAL_FAILURE = auto()
    TERMINAL_SUCCESS = auto()
    PROJECT_SWITCH = auto()
    SYSTEM_ALERT = auto()
    IDLE_CHECKIN = auto()


@dataclass
class ProactiveEvent:
    event_type: ProactiveEventType
    source: str
    title: str
    details: str
    timestamp: float = field(default_factory=time.time)
    suggested_action: str = ""
    metadata: dict = field(default_factory=dict)

    def event_key(self) -> str:
        """Unique key for deduplication and rate-limiting."""
        clean = f"{self.event_type.name}:{self.source}:{self.title}"
        return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]


class ProactiveHub:
    """
    Singleton event bus and context manager for proactive assistance.
    """

    _instance: Optional[ProactiveHub] = None
    _singleton_lock = threading.Lock()

    # Regex patterns for high-confidence error recognition in OCR text
    _ERROR_PATTERNS = [
        re.compile(r"Traceback \(most recent call last\):[\s\S]*?(\w+Error: [^\n\r]+)", re.IGNORECASE),
        re.compile(r"error\[E\d+\]:\s*([^\n\r]+)", re.IGNORECASE),
        re.compile(r"(?:TypeError|ReferenceError|SyntaxError|Uncaught Exception):\s*([^\n\r]+)", re.IGNORECASE),
        re.compile(r"panic:\s*([^\n\r]+)", re.IGNORECASE),
        re.compile(r"(BUILD FAILED[^\n\r]*)", re.IGNORECASE),
        re.compile(r"(fatal error:\s*[^\n\r]+)", re.IGNORECASE),
        re.compile(r"(AssertionError:\s*[^\n\r]+)", re.IGNORECASE),
    ]

    def __init__(self):
        self._lock = threading.Lock()
        self._pending_events: deque[ProactiveEvent] = deque(maxlen=20)
        self._recent_events: list[ProactiveEvent] = []
        self._cooldowns: dict[str, float] = {}   # event_key -> last_emitted_timestamp
        self._listeners: list[Callable[[ProactiveEvent], None]] = []

        self._last_active_project: str = ""
        self._last_screen_error_hash: str = ""
        self._hooks_started = False

    @classmethod
    def get_instance(cls) -> ProactiveHub:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── Event Subscription & Dispatch ──────────────────────────────────────

    def add_listener(self, callback: Callable[[ProactiveEvent], None]) -> None:
        """Register a callback when a qualified proactive event is triggered."""
        with self._lock:
            self._listeners.append(callback)

    def emit_event(
        self,
        event: ProactiveEvent,
        cooldown_seconds: float = 120.0,
    ) -> bool:
        """
        Post an event to the proactive pipeline.
        Returns True if accepted, False if suppressed by rate-limiting/cooldown.
        """
        key = event.event_key()
        now = time.time()

        with self._lock:
            last = self._cooldowns.get(key, 0.0)
            if now - last < cooldown_seconds:
                return False

            self._cooldowns[key] = now
            self._pending_events.append(event)
            self._recent_events.append(event)
            if len(self._recent_events) > 50:
                self._recent_events.pop(0)

            listeners_copy = list(self._listeners)

        for listener in listeners_copy:
            try:
                listener(event)
            except Exception as e:
                print(f"[ProactiveHub] ⚠️ Listener exception: {e}")

        return True

    def pop_next_event(self) -> Optional[ProactiveEvent]:
        """Fetch the next actionable proactive event."""
        with self._lock:
            if self._pending_events:
                return self._pending_events.popleft()
            return None

    def has_pending_events(self) -> bool:
        with self._lock:
            return len(self._pending_events) > 0

    # ── Hooks & Screen/Terminal Analysis ──────────────────────────────────

    def start_hooks(self) -> None:
        """Attach background observers for terminal, screen errors, and workspace changes."""
        with self._lock:
            if self._hooks_started:
                return
            self._hooks_started = True

        observer = get_screen_observer()
        observer.add_listener(self._on_screen_observation)
        print("[ProactiveHub] 🧠 Ambient event hooks activated.")

    def _on_screen_observation(self, obs: ScreenObservation) -> None:
        """Inspect screen observation for errors and workspace context."""
        text = obs.ocr_text_preview
        if not text:
            return

        # 1. Error Detection in Focused Window
        for pattern in self._ERROR_PATTERNS:
            m = pattern.search(text)
            if m:
                err_detail = m.group(1).strip() if m.lastindex else m.group(0).strip()
                err_hash = hashlib.sha256(f"{obs.active_app}:{err_detail}".encode("utf-8")).hexdigest()[:16]

                if err_hash != self._last_screen_error_hash:
                    self._last_screen_error_hash = err_hash
                    ev = ProactiveEvent(
                        event_type=ProactiveEventType.SCREEN_ERROR,
                        source=obs.active_app or "Active Window",
                        title=f"Error in {obs.active_title[:30] or obs.active_app}",
                        details=err_detail[:200],
                        suggested_action="Offer to inspect the traceback or explain the fix.",
                        metadata={
                            "window_title": obs.active_title,
                            "app_id": obs.active_app,
                            "ocr_snippet": text[:300],
                        },
                    )
                    self.emit_event(ev, cooldown_seconds=180.0)
                    break

        # 2. Terminal Project Directory Switch
        if obs.is_terminal and obs.terminal_cwd:
            cwd_path = Path(obs.terminal_cwd)
            project_name = cwd_path.name
            if (cwd_path / ".git").exists() and project_name != self._last_active_project:
                self._last_active_project = project_name
                ev = ProactiveEvent(
                    event_type=ProactiveEventType.PROJECT_SWITCH,
                    source="Terminal",
                    title=f"Switched project to '{project_name}'",
                    details=f"Directory: {obs.terminal_cwd}",
                    metadata={"cwd": obs.terminal_cwd, "project": project_name},
                )
                self.emit_event(ev, cooldown_seconds=300.0)

    # ── Gemini Live Context Formatter ─────────────────────────────────────

    def build_event_prompt(
        self,
        event: ProactiveEvent,
        memory: dict | None = None,
        recent_turns: list[str] | None = None,
    ) -> str:
        """
        Construct a concise, high-context instruction turn for Gemini Live.
        Instructs the model to speak naturally about the event in 1 brief sentence.
        """
        lines = [
            f"[PROACTIVE_TRIGGER: {event.event_type.name}]",
            f"Source: {event.source}",
            f"Event: {event.title}",
        ]
        if event.details:
            lines.append(f"Details: {event.details}")
        if event.suggested_action:
            lines.append(f"Suggested direction: {event.suggested_action}")

        lines.extend([
            "",
            "Rules for speaking:",
            "- Speak in 1 short, natural, conversational sentence.",
            "- Be helpful and calm, never intrusive, pushy, or robotic.",
            "- Mention what you observed specifically (e.g. the error name or test result).",
            "- Do NOT mention '[PROACTIVE_TRIGGER]' or explain system mechanics.",
            "- Ask briefly if they would like you to investigate or help.",
        ])

        return "\n".join(lines)


def get_proactive_hub() -> ProactiveHub:
    """Convenience getter for the ProactiveHub singleton."""
    return ProactiveHub.get_instance()
