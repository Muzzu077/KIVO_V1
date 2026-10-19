"""
core/screen_observer.py — Continuous background screen monitoring service.

WHY THIS EXISTS
    A reactive assistant only captures the screen when asked "what is on my screen?",
    which incurs latency, misses transient context (like error popups that disappear),
    and forces the user to manually tell the assistant to look.

    Continuous Screen Observer runs a lightweight background loop that:
      1. Periodically captures screen frames using grim (Wayland) / mss (fallback).
      2. Computes perceptual visual change (MSE / downsampled diff) so it consumes
         minimal CPU when the screen is static, and ramps up when the user interacts.
      3. Synchronizes with DesktopContextService to maintain real-time window,
         workspace, and active application state.
      4. Performs background OCR / text extraction on active regions to know what
         code, errors, or text the user is looking at right now.
      5. Maintains a rolling memory of recent screen changes and provides an
         instantaneous context summary to JARVIS and Gemini Live.
      6. Includes privacy controls (pause/resume/blackout) and adaptive throttling.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PIL import Image
import numpy as np

from core.desktop_context import get_desktop_context, WindowInfo


@dataclass
class ScreenObservation:
    timestamp: float = 0.0
    active_app: str = ""
    active_title: str = ""
    active_window_id: str | int = 0
    ocr_text_preview: str = ""
    change_score: float = 0.0        # 0.0 (identical) to 1.0 (completely new)
    frame_jpeg: bytes = b""          # latest compressed JPEG for Gemini Live
    is_terminal: bool = False
    is_browser: bool = False
    terminal_cwd: str = ""


class ScreenObserver:
    """
    Continuous background screen monitor.
    Thread-safe, self-throttling, low CPU usage.
    """

    _instance: Optional[ScreenObserver] = None
    _singleton_lock = threading.Lock()

    def __init__(
        self,
        interval_active: float = 2.0,   # Seconds between captures when user active
        interval_idle: float = 8.0,     # Seconds between captures when screen static
        change_threshold: float = 0.04, # Diff ratio required to trigger OCR / update
    ):
        self._interval_active = interval_active
        self._interval_idle = interval_idle
        self._change_threshold = change_threshold

        self._desktop_ctx = get_desktop_context()
        self._lock = threading.Lock()
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None

        self._prev_thumbnail: Optional[np.ndarray] = None
        self._last_observation: Optional[ScreenObservation] = None
        self._last_active_time = time.monotonic()

        self._ocr_cooldown: float = 6.0
        self._last_ocr_time: float = 0.0
        self._cached_ocr: str = ""
        self._has_tesseract: Optional[bool] = None

        # Listeners for screen change events: List of fn(ScreenObservation) -> None
        self._listeners: list[Callable[[ScreenObservation], None]] = []

    def _run_ocr(self, img: Image.Image, fw: Optional[WindowInfo] = None) -> str:
        """Bounded, downscaled, timeout-protected OCR to prevent CPU lockups."""
        if self._has_tesseract is None:
            self._has_tesseract = shutil.which("tesseract") is not None
        if not self._has_tesseract:
            return ""

        target_img = img
        if fw and fw.size and fw.size[0] > 50 and fw.size[1] > 50:
            w, h = img.size
            x, y = fw.position
            fw_w, fw_h = fw.size
            x1 = max(0, min(x, w - 1))
            y1 = max(0, min(y, h - 1))
            x2 = max(x1 + 10, min(x + fw_w, w))
            y2 = max(y1 + 10, min(y + fw_h, h))
            if x2 > x1 and y2 > y1:
                try:
                    target_img = img.crop((x1, y1, x2, y2))
                except Exception:
                    target_img = img

        try:
            import pytesseract
            if target_img.width > 1600:
                scale = 1600.0 / target_img.width
                new_h = max(10, int(target_img.height * scale))
                target_img = target_img.resize((1600, new_h), Image.Resampling.BILINEAR)
            return pytesseract.image_to_string(target_img, timeout=2.0)[:1000].strip()
        except Exception:
            return ""

    @classmethod
    def get_instance(cls) -> ScreenObserver:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background monitor thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._paused = False
            self._thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="jarvis-screen-observer"
            )
            self._thread.start()
            print("[ScreenObserver] 🚀 Background screen monitoring started.")

    def stop(self) -> None:
        """Stop the background monitor thread."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        print("[ScreenObserver] 🛑 Background screen monitoring stopped.")

    def pause(self) -> None:
        """Temporarily pause screen capture (privacy mode)."""
        with self._lock:
            self._paused = True
        print("[ScreenObserver] ⏸️ Screen monitoring paused (privacy mode).")

    def resume(self) -> None:
        """Resume screen capture."""
        with self._lock:
            self._paused = False
        print("[ScreenObserver] ▶️ Screen monitoring resumed.")

    def is_active(self) -> bool:
        with self._lock:
            return self._running and not self._paused

    def add_listener(self, callback: Callable[[ScreenObservation], None]) -> None:
        """Register a callback invoked whenever a significant screen change occurs."""
        with self._lock:
            self._listeners.append(callback)

    # ── Current State Access ───────────────────────────────────────────────

    def get_latest_observation(self) -> Optional[ScreenObservation]:
        """Return the most recent observation snapshot."""
        with self._lock:
            return self._last_observation

    def get_latest_frame(self) -> tuple[bytes, str]:
        """Return (jpeg_bytes, 'image/jpeg') for immediate injection into LLM."""
        with self._lock:
            if self._last_observation and self._last_observation.frame_jpeg:
                return self._last_observation.frame_jpeg, "image/jpeg"
        # If no frame yet in memory, take one synchronously
        return self._capture_frame_jpeg()

    def get_context_summary(self) -> str:
        """Formatted summary of what is happening on screen right now."""
        obs = self.get_latest_observation()
        if not obs:
            return "Screen observation: Initializing..."

        lines = [
            f"Active Application: {obs.active_app or 'Unknown'}",
            f"Window Title: \"{obs.active_title}\"",
        ]
        if obs.is_terminal:
            lines.append(f"State: Terminal emulator active (cwd: {obs.terminal_cwd or 'unknown'})")
        elif obs.is_browser:
            lines.append("State: Web browser active")

        if obs.ocr_text_preview:
            # Clean snippet (truncate to 400 chars)
            clean_text = " ".join(obs.ocr_text_preview.split())[:400]
            lines.append(f"Visible Content Preview: {clean_text}")

        age = time.monotonic() - obs.timestamp
        lines.append(f"Observed: {age:.1f}s ago (Visual Change Index: {obs.change_score:.2f})")
        return "\n".join(lines)

    # ── Internal Engine ────────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
                paused = self._paused

            if paused:
                time.sleep(1.0)
                continue

            start_t = time.monotonic()
            change_detected = False

            try:
                # 1. Grab raw screen frame
                pil_img = self._grab_screen_pil()
                if pil_img:
                    # 2. Compute visual difference using small thumbnail (64x64)
                    thumb = np.array(pil_img.resize((64, 64)).convert("L"), dtype=np.float32)
                    change_score = 0.0

                    if self._prev_thumbnail is not None:
                        # Normalized mean absolute pixel difference (0.0 to 1.0)
                        diff = np.abs(thumb - self._prev_thumbnail)
                        change_score = float(np.mean(diff) / 255.0)

                    self._prev_thumbnail = thumb

                    # 3. Check desktop context
                    snap = self._desktop_ctx.snapshot(max_age=1.0)
                    fw = snap.focused_window
                    app_id = fw.app_id if fw else ""
                    title = fw.title if fw else ""
                    wid = fw.window_id if fw else 0

                    is_term = self._desktop_ctx.is_terminal_focused()
                    is_browser = False
                    if fw:
                        b_apps = {"google-chrome", "chromium", "firefox", "brave-browser"}
                        is_browser = any(b in app_id.lower() for b in b_apps)

                    # Check if window switched or screen changed noticeably
                    prev = self._last_observation
                    window_switched = (prev is None or prev.active_window_id != wid or prev.active_title != title)
                    significant_change = change_score > self._change_threshold

                    ocr_snippet = ""
                    if window_switched or significant_change or prev is None:
                        change_detected = True
                        self._last_active_time = time.monotonic()

                        # Compress JPEG frame for fast LLM delivery (max 1280x720)
                        img_resized = pil_img.copy()
                        img_resized.thumbnail((1280, 720), Image.Resampling.BILINEAR)
                        buf = io.BytesIO()
                        img_resized.convert("RGB").save(buf, format="JPEG", quality=80)
                        jpeg_bytes = buf.getvalue()

                        # Bounded, debounced OCR only on changes
                        now_t = time.monotonic()
                        if window_switched or (now_t - self._last_ocr_time >= self._ocr_cooldown):
                            self._last_ocr_time = now_t
                            ocr_snippet = self._run_ocr(pil_img, fw)
                            self._cached_ocr = ocr_snippet
                        else:
                            ocr_snippet = self._cached_ocr

                        # Try getting terminal CWD if terminal
                        term_cwd = ""
                        if is_term:
                            try:
                                from core.terminal_control import get_terminal_controller
                                tc = get_terminal_controller()
                                term_cwd = tc.get_current_directory()
                            except Exception:
                                pass

                        new_obs = ScreenObservation(
                            timestamp=time.monotonic(),
                            active_app=app_id,
                            active_title=title,
                            active_window_id=wid,
                            ocr_text_preview=ocr_snippet,
                            change_score=change_score,
                            frame_jpeg=jpeg_bytes,
                            is_terminal=is_term,
                            is_browser=is_browser,
                            terminal_cwd=term_cwd,
                        )

                        with self._lock:
                            self._last_observation = new_obs

                        # Notify listeners
                        for cb in self._listeners:
                            try:
                                cb(new_obs)
                            except Exception as e:
                                print(f"[ScreenObserver] Listener error: {e}")

            except Exception as e:
                # Never crash the background monitor
                pass

            # Adaptive sleep: If active recently, sleep interval_active; if idle, slow down to interval_idle
            idle_time = time.monotonic() - self._last_active_time
            current_interval = self._interval_active if idle_time < 15.0 else self._interval_idle
            elapsed = time.monotonic() - start_t
            sleep_duration = max(0.2, current_interval - elapsed)
            time.sleep(sleep_duration)

    def _grab_screen_pil(self) -> Optional[Image.Image]:
        """Capture screen to PIL Image using grim (Wayland) or mss."""
        # 1. Grim on Wayland
        if shutil.which("grim") and sys.platform.startswith("linux"):
            try:
                r = subprocess.run(["grim", "-"], capture_output=True, timeout=3.0)
                if r.returncode == 0 and r.stdout:
                    return Image.open(io.BytesIO(r.stdout)).convert("RGB")
            except Exception:
                pass

        # 2. MSS Fallback
        try:
            import mss
            with mss.mss() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                shot = sct.grab(monitor)
                return Image.frombytes("RGB", shot.size, shot.rgb)
        except Exception:
            pass

        return None

    def _capture_frame_jpeg(self) -> tuple[bytes, str]:
        """Synchronous capture fallback."""
        img = self._grab_screen_pil()
        if not img:
            return b"", "image/jpeg"
        img.thumbnail((1280, 720), Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return buf.getvalue(), "image/jpeg"

    def capture_observation(self) -> Optional[ScreenObservation]:
        """Capture the screen immediately, run OCR, and return a fresh ScreenObservation."""
        pil_img = self._grab_screen_pil()
        if not pil_img:
            return None

        snap = self._desktop_ctx.refresh()
        fw = snap.focused_window
        app_id = fw.app_id if fw else "unknown"
        title = fw.title if fw else ""
        wid = fw.window_id if fw else 0

        is_term = self._desktop_ctx.is_terminal_focused()
        b_apps = {"google-chrome", "chromium", "firefox", "brave", "edge", "opera", "vivaldi"}
        is_browser = any(b in app_id.lower() for b in b_apps)

        img_resized = pil_img.copy()
        img_resized.thumbnail((1280, 720), Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        img_resized.convert("RGB").save(buf, format="JPEG", quality=80)
        jpeg_bytes = buf.getvalue()

        ocr_snippet = self._run_ocr(pil_img, fw)
        self._cached_ocr = ocr_snippet
        self._last_ocr_time = time.monotonic()

        term_cwd = ""
        if is_term:
            try:
                from core.terminal_control import get_terminal_controller
                tc = get_terminal_controller()
                term_cwd = tc.get_current_directory()
            except Exception:
                pass

        obs = ScreenObservation(
            timestamp=time.monotonic(),
            active_app=app_id,
            active_title=title,
            active_window_id=wid,
            ocr_text_preview=ocr_snippet,
            change_score=1.0,
            frame_jpeg=jpeg_bytes,
            is_terminal=is_term,
            is_browser=is_browser,
            terminal_cwd=term_cwd,
        )
        with self._lock:
            self._last_observation = obs
        return obs


def get_screen_observer() -> ScreenObserver:
    """Convenience getter for the ScreenObserver singleton."""
    return ScreenObserver.get_instance()
