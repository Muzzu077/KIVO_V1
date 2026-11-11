"""Immutable data models for default microphone mute state and operations.

Feature name: Default Microphone Mute.
This controls ONLY the WirePlumber default audio source.  It does not mute
other physical capture devices, monitor sources, virtual sources, application
streams, or KIVO's internal sounddevice capture.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import ClassVar

STATE_MUTED = "muted"
STATE_UNMUTED = "unmuted"
STATE_UNAVAILABLE = "unavailable"
STATE_ERROR = "error"

VALID_STATES = frozenset(
    {
        STATE_MUTED,
        STATE_UNMUTED,
        STATE_UNAVAILABLE,
        STATE_ERROR,
    }
)

# Error codes defined in the architecture specification §4.5
ERROR_WPCTL_MISSING = "wpctl_missing"
ERROR_WIREPLUMBER_UNAVAILABLE = "wireplumber_unavailable"
ERROR_NO_DEFAULT_SOURCE = "no_default_source"
ERROR_COMMAND_FAILED = "command_failed"
ERROR_COMMAND_TIMEOUT = "command_timeout"
ERROR_INVALID_OUTPUT = "invalid_output"
ERROR_VERIFICATION_FAILED = "verification_failed"
ERROR_DEVICE_CHANGED = "device_changed"
ERROR_BUSY = "busy"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_SERVICE_UNAVAILABLE = "service_unavailable"
ERROR_INTERNAL_ERROR = "internal_error"

STALE_AFTER_SECONDS = 12.0
_NAME_LIMIT = 128


def _sanitize_string(value: str, limit: int = _NAME_LIMIT) -> str:
    """Strip C0/C1 and bidi controls, collapse whitespace, and clamp length."""
    # Bidi controls: U+202A..U+202E and U+2066..U+2069
    stripped = re.sub(r"[\u202a-\u202e\u2066-\u2069\x00-\x1f\x7f-\x9f]", "", value)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return collapsed[:limit]


@dataclass(frozen=True)
class MicSource:
    """WirePlumber audio source identity for the default microphone."""

    id: int
    name: str
    description: str = "Default microphone"
    is_default: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.id, int) or self.id <= 0:
            raise ValueError("source node ID must be a positive integer")
        object.__setattr__(
            self,
            "name",
            _sanitize_string(self.name) or "Default microphone",
        )
        object.__setattr__(
            self,
            "description",
            _sanitize_string(self.description) or "Default microphone",
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "is_default": self.is_default,
        }


@dataclass(frozen=True, kw_only=True)
class MicState:
    """Current state of the WirePlumber default audio source."""

    state: str = STATE_UNAVAILABLE
    muted: bool | None = None
    verified: bool = False
    source: MicSource | None = None
    revision: int = 0
    changed_at: float = 0.0
    sampled_at: float | None = None
    sampled_monotonic: float | None = field(
        default=None, repr=False, compare=False
    )
    error_code: str | None = None
    error_message: str | None = None

    def age_seconds(self, now_monotonic: float | None = None) -> float | None:
        if self.sampled_monotonic is None:
            return None
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return max(0.0, float(now) - self.sampled_monotonic)

    def is_stale(self, now_monotonic: float | None = None) -> bool:
        if self.state not in (STATE_MUTED, STATE_UNMUTED):
            return False
        age = self.age_seconds(now_monotonic)
        return age is not None and age > STALE_AFTER_SECONDS

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        age = self.age_seconds(now_monotonic)
        return {
            "state": self.state,
            "muted": self.muted,
            "verified": self.verified,
            "source": self.source.to_dict() if self.source is not None else None,
            "revision": self.revision,
            "changed_at": round(self.changed_at, 3) if self.changed_at else 0.0,
            "sampled_at": round(self.sampled_at, 3) if self.sampled_at is not None else None,
            "age_seconds": round(age, 3) if age is not None else None,
            "stale": self.is_stale(now_monotonic),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def unavailable(
        cls,
        *,
        error_code: str = ERROR_WIREPLUMBER_UNAVAILABLE,
        error_message: str = "WirePlumber audio source unavailable",
        revision: int = 0,
        now: float | None = None,
        now_monotonic: float | None = None,
    ) -> "MicState":
        current_time = time.time() if now is None else now
        current_mono = time.monotonic() if now_monotonic is None else now_monotonic
        return cls(
            state=STATE_UNAVAILABLE,
            muted=None,
            verified=False,
            source=None,
            revision=revision,
            changed_at=current_time,
            sampled_at=current_time,
            sampled_monotonic=current_mono,
            error_code=error_code,
            error_message=error_message,
        )


@dataclass(frozen=True, kw_only=True)
class MicResult:
    """Result of an explicit set_mute operation on the default microphone."""

    ok: bool
    requested_muted: bool
    changed: bool
    default_source_changed: bool | None
    duration_ms: int
    state: MicState
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self, now_monotonic: float | None = None) -> dict:
        return {
            "ok": self.ok,
            "requested_muted": self.requested_muted,
            "changed": self.changed,
            "default_source_changed": self.default_source_changed,
            "duration_ms": self.duration_ms,
            "state": self.state.to_dict(now_monotonic),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


__all__ = [
    "STATE_MUTED",
    "STATE_UNMUTED",
    "STATE_UNAVAILABLE",
    "STATE_ERROR",
    "VALID_STATES",
    "ERROR_WPCTL_MISSING",
    "ERROR_WIREPLUMBER_UNAVAILABLE",
    "ERROR_NO_DEFAULT_SOURCE",
    "ERROR_COMMAND_FAILED",
    "ERROR_COMMAND_TIMEOUT",
    "ERROR_INVALID_OUTPUT",
    "ERROR_VERIFICATION_FAILED",
    "ERROR_DEVICE_CHANGED",
    "ERROR_BUSY",
    "ERROR_RATE_LIMITED",
    "ERROR_SERVICE_UNAVAILABLE",
    "ERROR_INTERNAL_ERROR",
    "STALE_AFTER_SECONDS",
    "MicSource",
    "MicState",
    "MicResult",
]
