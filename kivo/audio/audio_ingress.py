"""Internal audio ingress queue for completed microphone captures.

Provides a clean, bounded, thread-safe consumer interface for future Stage 7
speech-recognition and processing pipelines.

Retention policy:
    - Bounded queue (default: 10 completed captures).
    - When full, the oldest unconsumed capture is discarded to prevent
      unbounded memory growth.
    - Raw audio bytes are kept in memory only; they are never logged, never
      written to disk, and never transmitted over JSON IPC.
    - All buffers are cleared during backend shutdown.
"""
from __future__ import annotations

import collections
import threading
from typing import Optional

from kivo.audio.audio_models import AudioCapture


class AudioIngress:
    """Internal ingress buffer for completed AudioCapture payloads."""

    def __init__(self, maxsize: int = 10):
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._queue: collections.deque[AudioCapture] = collections.deque(maxlen=maxsize)
        self._history: collections.deque[dict] = collections.deque(maxlen=20)
        self._event = threading.Event()

    def submit(self, capture: AudioCapture) -> bool:
        """Submit a completed capture to the ingress queue.

        Thread-safe. Drops the oldest entry if maxsize is exceeded.
        """
        if not isinstance(capture, AudioCapture):
            raise TypeError("capture must be an instance of AudioCapture")
        if not capture.audio_bytes:
            return False

        with self._lock:
            self._queue.append(capture)
            self._history.append(capture.metadata())
            self._event.set()

        return True

    def get_next(self, timeout: Optional[float] = None) -> Optional[AudioCapture]:
        """Retrieve and remove the next completed capture (FIFO).

        Blocks up to timeout seconds if the queue is empty.
        Returns None if timed out or queue is empty.
        """
        deadline = None
        if timeout is not None:
            import time
            deadline = time.monotonic() + timeout

        while True:
            with self._lock:
                if self._queue:
                    item = self._queue.popleft()
                    if not self._queue:
                        self._event.clear()
                    return item

            # Queue was empty, wait for an item
            if timeout is None:
                self._event.wait()
            else:
                import time
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if not self._event.wait(timeout=remaining):
                    return None

    def peek_latest(self) -> Optional[AudioCapture]:
        """Return the most recently captured audio without removing it."""
        with self._lock:
            if self._queue:
                return self._queue[-1]
            return None

    def get_latest_metadata(self) -> Optional[dict]:
        """Return metadata for the most recent capture."""
        with self._lock:
            if self._history:
                return dict(self._history[-1])
            return None

    def get_history(self, limit: int = 10) -> list[dict]:
        """Return safe metadata history for recently completed captures."""
        with self._lock:
            items = list(self._history)
            return items[-limit:]

    def queue_size(self) -> int:
        """Number of unconsumed captures currently in the queue."""
        with self._lock:
            return len(self._queue)

    def clear(self) -> None:
        """Discard all queued audio and clear buffers during shutdown."""
        with self._lock:
            self._queue.clear()
            self._history.clear()
            self._event.clear()
