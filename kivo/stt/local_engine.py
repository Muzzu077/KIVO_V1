"""Local speech-to-text engine powered by faster-whisper (CTranslate2)."""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import numpy as np

from kivo.audio.audio_models import AudioCapture
from kivo.stt.engine import STTEngine
from kivo.stt.exceptions import STTAudioValidationError, STTInferenceError, STTModelNotFoundError
from kivo.stt.models import TranscriptionResult
from providers.audio import to_float32


def _default_cpu_threads() -> int:
    """Number of CPU threads to allocate for transcription inference."""
    try:
        count = os.cpu_count() or 4
    except Exception:
        count = 4
    return max(1, min(8, count // 2))


class FasterWhisperEngine(STTEngine):
    """Local Whisper engine using CTranslate2."""

    def __init__(
        self,
        model_name: str = "base",
        device: str = "cpu",
        compute_type: Optional[str] = None,
        cpu_threads: Optional[int] = None,
        download_root: Optional[str] = None,
    ):
        self._model_name = model_name
        self._device = device.lower()
        self._cpu_threads = cpu_threads or _default_cpu_threads()
        self._download_root = download_root
        self._lock = threading.Lock()
        self._model = None

        if compute_type is not None:
            self._compute_type = compute_type
        else:
            self._compute_type = "float16" if self._device.startswith("cuda") else "int8"

    def is_loaded(self) -> bool:
        with self._lock:
            return self._model is not None

    def get_model_name(self) -> str:
        return self._model_name

    def get_device(self) -> str:
        return self._device

    def load(self) -> None:
        """Load the faster-whisper model."""
        with self._lock:
            if self._model is not None:
                return

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTModelNotFoundError(
                "faster-whisper is not installed. Run: pip install faster-whisper"
            ) from exc

        try:
            model = WhisperModel(
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
                cpu_threads=self._cpu_threads,
                download_root=self._download_root,
            )
        except Exception as exc:
            # Fallback to CPU if CUDA initialization fails
            if self._device.startswith("cuda"):
                try:
                    self._device = "cpu"
                    self._compute_type = "int8"
                    model = WhisperModel(
                        self._model_name,
                        device="cpu",
                        compute_type="int8",
                        cpu_threads=self._cpu_threads,
                        download_root=self._download_root,
                    )
                except Exception as inner_exc:
                    raise STTModelNotFoundError(f"Failed to load STT model '{self._model_name}': {inner_exc}") from inner_exc
            else:
                raise STTModelNotFoundError(f"Failed to load STT model '{self._model_name}': {exc}") from exc

        with self._lock:
            self._model = model

    def unload(self) -> None:
        """Unload model and free resources."""
        with self._lock:
            self._model = None

    def transcribe(
        self,
        audio_capture: AudioCapture,
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe an AudioCapture into text."""
        if not isinstance(audio_capture, AudioCapture):
            raise STTAudioValidationError("audio_capture must be an AudioCapture instance")

        if not audio_capture.audio_bytes:
            return TranscriptionResult(
                capture_id=audio_capture.capture_id,
                text="",
                status="empty",
                engine="faster-whisper",
                model=self._model_name,
                duration=audio_capture.duration,
            )

        # Drop audio shorter than 0.25s (room tone/clicks) to prevent hallucinations
        if audio_capture.duration < 0.25 and len(audio_capture.audio_bytes) < 8000:
            return TranscriptionResult(
                capture_id=audio_capture.capture_id,
                text="",
                status="empty",
                engine="faster-whisper",
                model=self._model_name,
                duration=audio_capture.duration,
            )

        # Ensure model is loaded
        if not self.is_loaded():
            self.load()

        with self._lock:
            model = self._model

        if model is None:
            raise STTModelNotFoundError("STT model is not loaded")

        # Convert raw 16-bit PCM bytes to float32 samples in [-1.0, 1.0]
        try:
            samples = to_float32(audio_capture.audio_bytes)
        except Exception as exc:
            raise STTAudioValidationError(f"Cannot convert PCM bytes to float32: {exc}") from exc

        target_lang = None
        if language and language.strip() and language.strip().lower() not in ("auto", "none"):
            target_lang = language.strip().lower()

        t0 = time.monotonic()
        try:
            segments, info = model.transcribe(
                samples,
                language=target_lang,
                beam_size=1,  # Greedy for low latency
                vad_filter=True,  # Drop trailing silence from PTT release
                condition_on_previous_text=False,
            )
            # Generator iteration performs the actual inference
            recognized_text = " ".join(s.text.strip() for s in segments).strip()
            processing_time = time.monotonic() - t0
        except Exception as exc:
            raise STTInferenceError(f"Inference error during transcription: {exc}") from exc

        detected_lang = getattr(info, "language", None)
        lang_prob = float(getattr(info, "language_probability", 0.0) or 0.0)

        status = "completed" if recognized_text else "empty"
        return TranscriptionResult(
            capture_id=audio_capture.capture_id,
            text=recognized_text,
            language=detected_lang,
            language_probability=lang_prob,
            engine="faster-whisper",
            model=self._model_name,
            duration=audio_capture.duration,
            processing_time=processing_time,
            created_at=time.time(),
            status=status,
        )
