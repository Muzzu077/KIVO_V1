"""Custom exceptions for the KIVO STT subsystem."""
from __future__ import annotations


class STTError(Exception):
    """Base exception for all speech-to-text errors."""


class STTModelNotFoundError(STTError):
    """Raised when the requested STT model cannot be located or downloaded."""


class STTInferenceError(STTError):
    """Raised when inference fails during audio processing."""


class STTAudioValidationError(STTError):
    """Raised when the incoming audio payload is invalid or unprocessable."""
