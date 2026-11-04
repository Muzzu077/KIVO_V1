"""Microphone audio capture engine for Push-to-Talk interactions.

Manages sounddevice input streams, bounded audio chunk buffering, real-time
level measurement, mono conversion, 16 kHz resampling, and orderly cleanup.
"""
from __future__ import annotations

import collections
import threading
import time
import uuid
from typing import Callable, Optional

import numpy as np

from kivo.audio.audio_models import AudioCapture, CaptureStatus
from kivo.audio.device_manager import AudioDeviceManager
from providers.audio import MIC_RATE, SAMPLE_WIDTH, resample, to_mono, to_pcm16

# Maximum recording duration in seconds before automatic cutoff
DEFAULT_MAX_DURATION_SECONDS = 120.0
TARGET_SAMPLE_RATE = MIC_RATE  # 16000 Hz
TARGET_CHANNELS = 1            # Mono


class AudioCaptureManager:
    """Thread-safe microphone capture manager for Push-to-Talk."""

    def __init__(
        self,
        device_manager: AudioDeviceManager,
        max_duration: float = DEFAULT_MAX_DURATION_SECONDS,
        on_state_change: Optional[Callable[[CaptureStatus], None]] = None,
        on_level: Optional[Callable[[float, float], None]] = None,
        on_complete: Optional[Callable[[AudioCapture], None]] = None,
    ):
        self._device_manager = device_manager
        self._max_duration = max(5.0, float(max_duration))
        self._on_state_change = on_state_change
        self._on_level = on_level
        self._on_complete = on_complete

        self._lock = threading.Lock()
        self._status = CaptureStatus(state="idle")

        self._stream = None
        self._chunks: collections.deque[bytes] = collections.deque()
        self._start_time: float = 0.0
        self._capture_id: str = ""
        self._active_device_name: str = ""
        self._stream_rate: int = TARGET_SAMPLE_RATE
        self._stream_channels: int = TARGET_CHANNELS
        self._timer: Optional[threading.Timer] = None
        self._closed: bool = False

    def get_status(self) -> CaptureStatus:
        """Return the current capture status snapshot."""
        with self._lock:
            duration = 0.0
            if self._status.state == "capturing" and self._start_time > 0:
                duration = time.monotonic() - self._start_time
            return CaptureStatus(
                state=self._status.state,
                duration=duration,
                device_name=self._status.device_name,
                peak_amplitude=self._status.peak_amplitude,
                rms_amplitude=self._status.rms_amplitude,
                error_message=self._status.error_message,
            )

    def start_capture(self, device_name: Optional[str] = None) -> bool:
        """Start microphone capture.

        Returns True if capture started, False if already capturing or on error.
        """
        import sounddevice as sd

        with self._lock:
            if self._closed:
                return False
            if self._status.state in ("capturing", "starting"):
                return False

            self._status.state = "starting"
            self._status.error_message = ""
            self._status.peak_amplitude = 0.0
            self._status.rms_amplitude = 0.0
            self._chunks.clear()
            self._capture_id = str(uuid.uuid4())

        self._notify_state()

        target_name = device_name
        if target_name is None:
            target_name = self._device_manager.get_selected_device_name()

        device_idx = self._device_manager.resolve_device_index(target_name)
        active_name = target_name or "System default"

        # Determine best sample rate and channels supported by device
        rate, channels = self._probe_format(sd, device_idx)
        if rate is None:
            with self._lock:
                self._status.state = "error"
                self._status.error_message = "Cannot open microphone with a supported audio format."
            self._notify_state()
            return False

        try:
            stream = sd.InputStream(
                samplerate=rate,
                channels=channels,
                dtype="int16",
                device=device_idx,
                blocksize=1024,
                callback=self._audio_callback,
            )
            stream.start()
        except Exception as exc:
            with self._lock:
                self._status.state = "error"
                self._status.error_message = f"Failed to open audio input: {exc}"
            self._notify_state()
            return False

        with self._lock:
            self._stream = stream
            self._stream_rate = rate
            self._stream_channels = channels
            self._active_device_name = active_name
            self._start_time = time.monotonic()
            self._status.state = "capturing"
            self._status.device_name = active_name

            # Auto-cutoff timer to prevent unbounded recording
            self._timer = threading.Timer(self._max_duration, self._on_timeout)
            self._timer.daemon = True
            self._timer.start()

        self._notify_state()
        return True

    def _probe_format(self, sd, device_idx: Optional[int]) -> tuple[Optional[int], int]:
        """Try standard rates and channel counts to find an opening stream."""
        rates_to_try = [TARGET_SAMPLE_RATE, 48000, 44100]
        channels_to_try = [1, 2]

        for r in rates_to_try:
            for ch in channels_to_try:
                try:
                    s = sd.InputStream(
                        samplerate=r,
                        channels=ch,
                        dtype="int16",
                        device=device_idx,
                        blocksize=1024,
                    )
                    s.close()
                    return r, ch
                except Exception:
                    continue
        return None, 1

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        """Called by PortAudio audio thread for each block of samples."""
        if not self._stream:
            return

        # indata can be int16 numpy array or bytes (e.g. in test)
        if hasattr(indata, "tobytes"):
            raw_bytes = indata.tobytes()
            data_arr = indata.astype(np.float32) / 32768.0
        else:
            raw_bytes = bytes(indata)
            data_arr = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        # Compute level metrics (0.0 to 1.0)
        try:
            peak = float(np.max(np.abs(data_arr))) if data_arr.size > 0 else 0.0
            rms = float(np.sqrt(np.mean(data_arr**2))) if data_arr.size > 0 else 0.0
        except Exception:
            peak, rms = 0.0, 0.0

        with self._lock:
            if self._status.state == "capturing":
                self._chunks.append(raw_bytes)
                self._status.peak_amplitude = peak
                self._status.rms_amplitude = rms

        if self._on_level is not None:
            try:
                self._on_level(peak, rms)
            except Exception:
                pass

    def stop_capture(self) -> Optional[AudioCapture]:
        """Stop capturing and return the processed 16 kHz mono AudioCapture."""
        with self._lock:
            if self._status.state != "capturing":
                return None
            self._status.state = "stopping"
            timer = self._timer
            self._timer = None
            stream = self._stream
            self._stream = None
            raw_chunks = list(self._chunks)
            self._chunks.clear()
            rate = self._stream_rate
            channels = self._stream_channels
            device_name = self._active_device_name
            capture_id = self._capture_id
            duration = max(0.0, time.monotonic() - self._start_time)

        if timer:
            timer.cancel()

        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

        # Assemble and convert audio to 16 kHz mono 16-bit PCM
        pcm_data = b"".join(raw_chunks)
        if not pcm_data:
            with self._lock:
                self._status.state = "idle"
            self._notify_state()
            return None

        try:
            # 1. Convert channels to mono if recorded in multi-channel
            if channels > 1:
                pcm_data = to_mono(pcm_data, channels)

            # 2. Resample to 16 kHz if recorded at a different rate
            if rate != TARGET_SAMPLE_RATE:
                pcm_data = resample(pcm_data, rate, TARGET_SAMPLE_RATE)

            actual_duration = len(pcm_data) / (TARGET_SAMPLE_RATE * SAMPLE_WIDTH * TARGET_CHANNELS)

            capture = AudioCapture(
                capture_id=capture_id,
                audio_bytes=pcm_data,
                samplerate=TARGET_SAMPLE_RATE,
                channels=TARGET_CHANNELS,
                encoding="pcm_s16le",
                duration=actual_duration,
                timestamp=time.time(),
                device_name=device_name,
            )
        except Exception as exc:
            with self._lock:
                self._status.state = "error"
                self._status.error_message = f"Audio processing error: {exc}"
            self._notify_state()
            return None

        with self._lock:
            self._status.state = "idle"
            self._status.peak_amplitude = 0.0
            self._status.rms_amplitude = 0.0

        self._notify_state()

        if self._on_complete is not None:
            try:
                self._on_complete(capture)
            except Exception:
                pass

        return capture

    def cancel_capture(self) -> bool:
        """Cancel the current capture without delivering audio."""
        with self._lock:
            if self._status.state not in ("capturing", "starting"):
                return False
            self._status.state = "stopping"
            timer = self._timer
            self._timer = None
            stream = self._stream
            self._stream = None
            self._chunks.clear()

        if timer:
            timer.cancel()

        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

        with self._lock:
            self._status.state = "idle"
            self._status.peak_amplitude = 0.0
            self._status.rms_amplitude = 0.0

        self._notify_state()
        return True

    def _on_timeout(self) -> None:
        """Called when maximum recording duration is reached."""
        self.stop_capture()

    def _notify_state(self) -> None:
        if self._on_state_change is not None:
            try:
                self._on_state_change(self.get_status())
            except Exception:
                pass

    def close(self) -> None:
        """Release all audio stream resources and clear buffers."""
        with self._lock:
            self._closed = True
        self.cancel_capture()
