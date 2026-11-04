"""Abstract base class for speech-to-text engines."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from kivo.audio.audio_models import AudioCapture
from kivo.stt.models import TranscriptionResult


class STTEngine(ABC):
    """Abstract interface for local and remote STT engines."""

    @abstractmethod
    def load(self) -> None:
        """Load model weights and initialize inference runtime."""

    @abstractmethod
    def unload(self) -> None:
        """Release model weights and free memory."""

    @abstractmethod
    def is_loaded(self) -> bool:
        """Whether the model is loaded and ready for inference."""

    @abstractmethod
    def transcribe(
        self,
        audio_capture: AudioCapture,
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe an AudioCapture into recognized text."""

    @abstractmethod
    def get_model_name(self) -> str:
        """Name of the active model."""

    @abstractmethod
    def get_device(self) -> str:
        """Execution device ('cpu' or 'cuda')."""
