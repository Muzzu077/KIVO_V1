"""KIVO Speech-to-Text (STT) transcription subsystem."""
from __future__ import annotations

from kivo.stt.engine import STTEngine
from kivo.stt.exceptions import STTAudioValidationError, STTError, STTInferenceError, STTModelNotFoundError
from kivo.stt.local_engine import FasterWhisperEngine
from kivo.stt.models import STTStatus, TranscriptionResult
from kivo.stt.stt_config import load_stt_config, save_stt_config
from kivo.stt.transcription_manager import TranscriptionManager

__all__ = [
    "STTEngine",
    "FasterWhisperEngine",
    "TranscriptionManager",
    "TranscriptionResult",
    "STTStatus",
    "STTError",
    "STTModelNotFoundError",
    "STTInferenceError",
    "STTAudioValidationError",
    "load_stt_config",
    "save_stt_config",
]
