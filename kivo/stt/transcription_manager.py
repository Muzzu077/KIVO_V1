"""Coordinates AudioIngress consumption, model inference, and event publishing."""
from __future__ import annotations

import collections
import threading
import time
from typing import Callable, Optional

from kivo.audio.audio_ingress import AudioIngress
from kivo.audio.audio_models import AudioCapture
from kivo.stt.engine import STTEngine
from kivo.stt.local_engine import FasterWhisperEngine
from kivo.stt.models import STTStatus, TranscriptionResult
from kivo.stt.stt_config import load_stt_config, save_stt_config


class TranscriptionManager:
    """Manages background speech recognition from AudioIngress."""

    def __init__(
        self,
        ingress: AudioIngress,
        engine: Optional[STTEngine] = None,
        language: str = "auto",
        on_status_change: Optional[Callable[[STTStatus], None]] = None,
        on_transcription_started: Optional[Callable[[str], None]] = None,
        on_transcription_completed: Optional[Callable[[TranscriptionResult], None]] = None,
        on_transcription_failed: Optional[Callable[[str], None]] = None,
    ):
        self._ingress = ingress
        self._language = language
        self._on_status_change = on_status_change
        self._on_transcription_started = on_transcription_started
        self._on_transcription_completed = on_transcription_completed
        self._on_transcription_failed = on_transcription_failed

        # Load persisted configuration if available
        cfg = load_stt_config()
        self._language = cfg.get("language", language) or "auto"
        model_name = cfg.get("model", "base")
        device = cfg.get("device", "cpu")

        if engine is not None:
            self._engine = engine
        else:
            self._engine = FasterWhisperEngine(model_name=model_name, device=device)

        self._lock = threading.Lock()
        self._state = "idle"
        self._error_message = ""
        self._history: collections.deque[TranscriptionResult] = collections.deque(maxlen=30)
        self._last_result: Optional[TranscriptionResult] = None

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._started = False

    def get_status(self) -> STTStatus:
        """Return the current STT state and queue size."""
        with self._lock:
            state = self._state
            last_res = self._last_result.to_dict() if self._last_result else None
            err = self._error_message

        queue_size = self._ingress.queue_size()
        return STTStatus(
            state=state,
            engine=self._engine.get_model_name() if self._engine else "none",
            model=self._engine.get_model_name() if self._engine else "none",
            language=self._language,
            available=self._engine is not None,
            device=self._engine.get_device() if self._engine else "cpu",
            queue_size=queue_size,
            last_result=last_res,
            error_message=err,
        )

    def get_history(self, limit: int = 10) -> list[dict]:
        """Return recently completed transcriptions."""
        with self._lock:
            items = [res.to_dict() for res in self._history]
            return items[-limit:]

    def get_latest_result(self) -> Optional[TranscriptionResult]:
        with self._lock:
            return self._last_result

    def set_language(self, language: str) -> None:
        """Update language preference ('auto' or two-letter code like 'en', 'hi', 'te')."""
        clean_lang = (language or "auto").strip().lower()
        with self._lock:
            self._language = clean_lang
        save_stt_config(language=clean_lang)
        self._notify_status()

    def start(self) -> None:
        """Start the background AudioIngress consumer worker thread."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="kivo-stt-worker",
                daemon=True,
            )
            self._worker_thread.start()

    def stop(self) -> None:
        """Signal worker to stop and wait for completion."""
        self._stop_event.set()
        worker = self._worker_thread
        if worker and worker.is_alive():
            worker.join(timeout=3.0)
        with self._lock:
            self._started = False
            self._state = "idle"
            if self._engine:
                try:
                    self._engine.unload()
                except Exception:
                    pass

    def _set_state(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._error_message = error
        self._notify_status()

    def _notify_status(self) -> None:
        if self._on_status_change is not None:
            try:
                self._on_status_change(self.get_status())
            except Exception:
                pass

    def _worker_loop(self) -> None:
        """Background loop reading from AudioIngress and invoking STT engine."""
        while not self._stop_event.is_set():
            try:
                # Wait for next completed capture with 0.5s timeout
                capture = self._ingress.get_next(timeout=0.5)
                if capture is None:
                    continue

                if self._stop_event.is_set():
                    break

                self._process_capture(capture)

            except Exception as exc:
                self._set_state("failed", str(exc))
                time.sleep(0.5)

    def _process_capture(self, capture: AudioCapture) -> None:
        """Process one completed recording through the engine."""
        capture_id = capture.capture_id
        if self._on_transcription_started is not None:
            try:
                self._on_transcription_started(capture_id)
            except Exception:
                pass

        self._set_state("transcribing")

        with self._lock:
            lang = self._language

        try:
            result = self._engine.transcribe(capture, language=lang)
        except Exception as exc:
            err_msg = str(exc)
            result = TranscriptionResult(
                capture_id=capture_id,
                text="",
                status="failed",
                error_message=err_msg,
                duration=capture.duration,
            )
            self._set_state("failed", err_msg)
            if self._on_transcription_failed is not None:
                try:
                    self._on_transcription_failed(err_msg)
                except Exception:
                    pass
        else:
            final_state = "completed" if result.text else "empty"
            self._set_state(final_state)

        with self._lock:
            self._last_result = result
            self._history.append(result)

        if result.status == "completed" and self._on_transcription_completed is not None:
            try:
                self._on_transcription_completed(result)
            except Exception:
                pass

        # Return to idle after a brief moment
        time.sleep(0.1)
        if not self._stop_event.is_set():
            with self._lock:
                if self._state in ("completed", "empty"):
                    self._state = "idle"
            self._notify_status()
