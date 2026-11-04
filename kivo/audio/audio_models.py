"""Data models for KIVO audio devices, captures, and status."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class AudioDevice:
    """An audio input device."""

    name: str
    hostapi: str
    max_input_channels: int
    default_samplerate: float
    is_default: bool = False
    is_available: bool = True
    index: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hostapi": self.hostapi,
            "max_input_channels": self.max_input_channels,
            "default_samplerate": self.default_samplerate,
            "is_default": self.is_default,
            "is_available": self.is_available,
            "index": self.index,
        }


@dataclass(frozen=True)
class AudioCapture:
    """Completed audio capture from microphone input."""

    capture_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    audio_bytes: bytes = b""
    samplerate: int = 16000
    channels: int = 1
    encoding: str = "pcm_s16le"
    duration: float = 0.0
    timestamp: float = field(default_factory=time.time)
    device_name: str = ""

    @property
    def byte_length(self) -> int:
        return len(self.audio_bytes)

    def metadata(self) -> dict:
        """Safe metadata dictionary without raw audio bytes.

        Never exposes raw audio payloads over IPC or in log messages.
        """
        return {
            "capture_id": self.capture_id,
            "samplerate": self.samplerate,
            "channels": self.channels,
            "encoding": self.encoding,
            "duration": round(self.duration, 3),
            "timestamp": round(self.timestamp, 3),
            "device_name": self.device_name,
            "byte_length": self.byte_length,
        }


@dataclass
class CaptureStatus:
    """Live status of the audio capture engine."""

    state: str = "idle"  # "idle" | "starting" | "capturing" | "stopping" | "error"
    duration: float = 0.0
    device_name: str = ""
    peak_amplitude: float = 0.0
    rms_amplitude: float = 0.0
    error_message: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "duration": round(self.duration, 2),
            "device_name": self.device_name,
            "peak_amplitude": round(self.peak_amplitude, 4),
            "rms_amplitude": round(self.rms_amplitude, 4),
            "error_message": self.error_message,
        }
