"""Data models for KIVO speech-to-text (STT) transcription."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class TranscriptionResult:
    """Structured result of a speech-to-text transcription."""

    transcription_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    capture_id: str = ""
    text: str = ""
    language: Optional[str] = None
    language_probability: float = 0.0
    engine: str = "faster-whisper"
    model: str = "base"
    duration: float = 0.0
    processing_time: float = 0.0
    created_at: float = field(default_factory=time.time)
    status: str = "completed"  # "completed" | "empty" | "failed" | "cancelled"
    error_message: str = ""

    def to_dict(self) -> dict:
        """Safe serialization for IPC and Dashboard display.

        Caps text length defensively to prevent unbounded IPC message payloads.
        """
        # Defensive cap: max 8000 characters
        capped_text = self.text[:8000] if self.text else ""
        return {
            "transcription_id": self.transcription_id,
            "capture_id": self.capture_id,
            "text": capped_text,
            "language": self.language,
            "language_probability": round(self.language_probability, 3),
            "engine": self.engine,
            "model": self.model,
            "duration": round(self.duration, 3),
            "processing_time": round(self.processing_time, 3),
            "created_at": round(self.created_at, 3),
            "status": self.status,
            "error_message": self.error_message,
        }


@dataclass
class STTStatus:
    """Live status snapshot of the transcription engine and queue."""

    state: str = "idle"  # "idle" | "queued" | "loading" | "transcribing" | "completed" | "empty" | "failed"
    engine: str = "faster-whisper"
    model: str = "base"
    language: str = "auto"
    available: bool = True
    device: str = "cpu"
    queue_size: int = 0
    last_result: Optional[dict] = None
    error_message: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "engine": self.engine,
            "model": self.model,
            "language": self.language,
            "available": self.available,
            "device": self.device,
            "queue_size": self.queue_size,
            "last_result": self.last_result,
            "error_message": self.error_message,
        }
