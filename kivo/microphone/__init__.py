"""Default Microphone Mute service for WirePlumber.

Controls ONLY the default audio source.  Does not guarantee system-wide mute
across all physical microphones, monitor sources, virtual sources, or KIVO's
internal push-to-talk capture.
"""
from __future__ import annotations

from kivo.microphone.models import (
    ERROR_BUSY,
    ERROR_COMMAND_FAILED,
    ERROR_COMMAND_TIMEOUT,
    ERROR_DEVICE_CHANGED,
    ERROR_INTERNAL_ERROR,
    ERROR_INVALID_OUTPUT,
    ERROR_NO_DEFAULT_SOURCE,
    ERROR_RATE_LIMITED,
    ERROR_SERVICE_UNAVAILABLE,
    ERROR_VERIFICATION_FAILED,
    ERROR_WIREPLUMBER_UNAVAILABLE,
    ERROR_WPCTL_MISSING,
    STATE_ERROR,
    STATE_MUTED,
    STATE_UNAVAILABLE,
    STATE_UNMUTED,
    VALID_STATES,
    MicResult,
    MicSource,
    MicState,
)
from kivo.microphone.service import MicrophoneService
from kivo.microphone.wireplumber import VolumeReading, WpctlRunner

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
    "MicSource",
    "MicState",
    "MicResult",
    "MicrophoneService",
    "VolumeReading",
    "WpctlRunner",
]
