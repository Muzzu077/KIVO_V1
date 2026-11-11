"""Default Microphone Mute service.

Manages state polling, serialized explicit mute/unmute operations, post-operation
verification, and default-source change detection using WirePlumber's wpctl.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from kivo.microphone.models import (
    ERROR_BUSY,
    ERROR_DEVICE_CHANGED,
    ERROR_INTERNAL_ERROR,
    ERROR_NO_DEFAULT_SOURCE,
    ERROR_RATE_LIMITED,
    ERROR_SERVICE_UNAVAILABLE,
    ERROR_VERIFICATION_FAILED,
    STATE_ERROR,
    STATE_MUTED,
    STATE_UNAVAILABLE,
    STATE_UNMUTED,
    MicResult,
    MicSource,
    MicState,
)
from kivo.microphone.wireplumber import VolumeReading, WpctlRunner

DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_IDENTITY_INTERVAL = 30.0
DEFAULT_STATE_TTL = 2.0
DEFAULT_RATE_LIMIT = 0.25
_LOCK_TIMEOUT = 0.5


class MicrophoneService:
    """Service controlling the WirePlumber default audio source mute state."""

    def __init__(
        self,
        on_state_changed: Callable[[MicState], None] | None = None,
        *,
        runner: WpctlRunner | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        identity_interval: float = DEFAULT_IDENTITY_INTERVAL,
        state_ttl: float = DEFAULT_STATE_TTL,
        rate_limit: float = DEFAULT_RATE_LIMIT,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self._on_state_changed = on_state_changed
        self._runner = runner if runner is not None else WpctlRunner()
        self._poll_interval = float(poll_interval)
        self._identity_interval = float(identity_interval)
        self._state_ttl = float(state_ttl)
        self._rate_limit = float(rate_limit)
        self._clock = clock
        self._wall_clock = wall_clock

        self._op_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._started = False
        self._running = False
        self._closed = False
        self._revision = 0
        self._last_set_mono = -1.0
        self._next_identity_mono = 0.0

        # Initial state is unavailable until the first read
        now_wall = self._wall_clock()
        now_mono = self._clock()
        self._state = MicState.unavailable(
            revision=0,
            now=now_wall,
            now_monotonic=now_mono,
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the background poll thread.

        Returns ``False`` if already closed.
        """
        if self._closed:
            return False
        if self._running:
            return True

        self._started = True
        self._running = True
        self._stop_event.clear()

        # Prime the state with an initial read before starting the thread
        self._poll_step()

        self._thread = threading.Thread(
            target=self._thread_main,
            name="kivo-microphone",
            daemon=True,
        )
        self._thread.start()
        return True

    def close(self, timeout: float = 3.0) -> None:
        """Stop background polling and close the service.

        Bounded join. Never unmutes or mutes on shutdown.
        """
        if self._closed:
            return
        self._closed = True
        self._running = False
        self._stop_event.set()

        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and threading.current_thread() is not thread
        ):
            thread.join(timeout=max(0.1, float(timeout)))

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(self._running and thread is not None and thread.is_alive())

    @property
    def thread_name(self) -> str | None:
        thread = self._thread
        return thread.name if thread is not None else None

    # ── Public Queries ─────────────────────────────────────────────────────

    def get_state(self) -> MicState:
        """Return the current mute state, reading from hardware if cache expired."""
        if self._closed:
            return self._state

        now_mono = self._clock()
        with self._state_lock:
            current = self._state

        age = current.age_seconds(now_mono)
        if (
            age is not None
            and age <= self._state_ttl
            and current.state in (STATE_MUTED, STATE_UNMUTED)
        ):
            return current

        # Cache expired or state not verified/ok: perform one bounded poll step
        self._poll_step()
        with self._state_lock:
            return self._state

    # ── Explicit Set Mute ──────────────────────────────────────────────────

    def set_mute(self, muted: bool) -> MicResult:
        """Explicitly set default microphone mute state (True=mute, False=unmute).

        Serialized, rate-limited, pinned to the resolved node ID, and verified.
        """
        start_time = self._clock()
        if self._closed:
            return self._make_result(
                ok=False,
                requested=muted,
                changed=False,
                default_source_changed=False,
                duration_ms=0,
                state=self._state,
                error_code=ERROR_SERVICE_UNAVAILABLE,
                error_message="Microphone service is closed or unavailable",
            )

        acquired = self._op_lock.acquire(timeout=_LOCK_TIMEOUT)
        if not acquired:
            return self._make_result(
                ok=False,
                requested=muted,
                changed=False,
                default_source_changed=False,
                duration_ms=int((self._clock() - start_time) * 1000),
                state=self.get_state(),
                error_code=ERROR_BUSY,
                error_message="Another microphone operation is in progress",
            )

        try:
            return self._execute_set_mute(muted, start_time)
        except Exception as exc:
            return self._make_result(
                ok=False,
                requested=muted,
                changed=False,
                default_source_changed=False,
                duration_ms=int((self._clock() - start_time) * 1000),
                state=self.get_state(),
                error_code=ERROR_INTERNAL_ERROR,
                error_message=f"Unexpected error during set_mute: {exc}",
            )
        finally:
            self._op_lock.release()

    # ── Internal Set Mute Flow ─────────────────────────────────────────────

    def _execute_set_mute(self, desired: bool, start_mono: float) -> MicResult:
        now_mono = self._clock()
        if self._last_set_mono > 0 and (now_mono - self._last_set_mono) < self._rate_limit:
            return self._make_result(
                ok=False,
                requested=desired,
                changed=False,
                default_source_changed=False,
                duration_ms=int((now_mono - start_mono) * 1000),
                state=self.get_state(),
                error_code=ERROR_RATE_LIMITED,
                error_message="Microphone operations rate limited (max 4 ops/sec)",
            )
        self._last_set_mono = now_mono

        # 1. Resolve default source
        source, err_code, err_msg = self._runner.list_default_source()
        if source is None or err_code is not None:
            new_state = self._update_state(
                state=STATE_UNAVAILABLE,
                muted=None,
                verified=False,
                source=None,
                error_code=err_code or ERROR_NO_DEFAULT_SOURCE,
                error_message=err_msg or "Failed to resolve default microphone",
            )
            return self._make_result(
                ok=False,
                requested=desired,
                changed=False,
                default_source_changed=False,
                duration_ms=int((self._clock() - start_mono) * 1000),
                state=new_state,
                error_code=err_code or ERROR_NO_DEFAULT_SOURCE,
                error_message=err_msg or "Failed to resolve default microphone",
            )

        pinned_id = source.id
        pinned_name = source.name

        # 2. Read pre-state of pinned node ID
        pre_reading, pre_err, _ = self._runner.get_volume(pinned_id)
        pre_verified = (pre_reading is not None)
        pre_muted = pre_reading.muted if pre_verified else None

        # 3. Check if already in desired state
        if pre_verified and pre_muted == desired:
            # Already in requested state; verify default didn't change
            post_source, post_err, _ = self._runner.list_default_source()
            default_changed = (
                post_source is not None and (post_source.id != pinned_id or post_source.name != pinned_name)
            ) if post_source else (None if post_err else False)

            if default_changed is True:
                # Default changed while checking!
                return self._handle_device_changed(desired, post_source, start_mono)

            # Verified match without needing to run set-mute
            target_state = STATE_MUTED if desired else STATE_UNMUTED
            new_state = self._update_state(
                state=target_state,
                muted=desired,
                verified=True,
                source=source,
            )
            return self._make_result(
                ok=True,
                requested=desired,
                changed=False,
                default_source_changed=False,
                duration_ms=int((self._clock() - start_mono) * 1000),
                state=new_state,
            )

        # 4. Apply explicit mute operation to pinned node ID
        cmd_ok, cmd_err, cmd_msg = self._runner.set_mute(pinned_id, desired)

        # 5. Pinned verification read (always performed, even if command failed)
        post_reading, read_err, read_msg = self._runner.get_volume(pinned_id)
        post_verified = (post_reading is not None)
        post_muted = post_reading.muted if post_verified else None

        # 6. Check if default source changed during operation
        post_source, list_err, _ = self._runner.list_default_source()
        if post_source is not None and (post_source.id != pinned_id or post_source.name != pinned_name):
            return self._handle_device_changed(desired, post_source, start_mono)

        default_changed_flag = False if post_source is not None else None

        # 7. Check if post-state matches requested
        if post_verified and post_muted == desired:
            target_state = STATE_MUTED if desired else STATE_UNMUTED
            changed = (pre_muted != desired) if pre_verified else True
            new_state = self._update_state(
                state=target_state,
                muted=desired,
                verified=True,
                source=source,
            )
            return self._make_result(
                ok=True,
                requested=desired,
                changed=changed,
                default_source_changed=default_changed_flag,
                duration_ms=int((self._clock() - start_mono) * 1000),
                state=new_state,
            )

        # Verification failed or mismatch
        fail_code = cmd_err or read_err or ERROR_VERIFICATION_FAILED
        fail_msg = cmd_msg or read_msg or f"Verification failed: expected muted={desired}, got {post_muted}"
        err_state = STATE_ERROR
        new_state = self._update_state(
            state=err_state,
            muted=post_muted,
            verified=post_verified,
            source=source,
            error_code=fail_code,
            error_message=fail_msg,
        )
        return self._make_result(
            ok=False,
            requested=desired,
            changed=False,
            default_source_changed=default_changed_flag,
            duration_ms=int((self._clock() - start_mono) * 1000),
            state=new_state,
            error_code=fail_code,
            error_message=fail_msg,
        )

    def _handle_device_changed(
        self, desired: bool, new_source: MicSource, start_mono: float
    ) -> MicResult:
        # Read the new default's current state
        reading, _, _ = self._runner.get_volume(new_source.id)
        is_muted = reading.muted if reading else None
        state_str = (
            (STATE_MUTED if is_muted else STATE_UNMUTED)
            if is_muted is not None
            else STATE_UNAVAILABLE
        )
        new_state = self._update_state(
            state=state_str,
            muted=is_muted,
            verified=(reading is not None),
            source=new_source,
            error_code=ERROR_DEVICE_CHANGED,
            error_message="Default audio source changed during operation",
        )
        return self._make_result(
            ok=False,
            requested=desired,
            changed=False,
            default_source_changed=True,
            duration_ms=int((self._clock() - start_mono) * 1000),
            state=new_state,
            error_code=ERROR_DEVICE_CHANGED,
            error_message="Default audio source changed during operation",
        )

    # ── State Updates and Notifications ────────────────────────────────────

    def _update_state(
        self,
        *,
        state: str,
        muted: bool | None,
        verified: bool,
        source: MicSource | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> MicState:
        now_wall = self._wall_clock()
        now_mono = self._clock()

        with self._state_lock:
            prev = self._state
            # Increment revision when state, muted, source ID, or availability changes
            state_changed = (
                prev.state != state
                or prev.muted != muted
                or (prev.source is None) != (source is None)
                or (prev.source is not None and source is not None and prev.source.id != source.id)
            )

            new_revision = prev.revision + 1 if state_changed else prev.revision
            changed_at = now_wall if state_changed else prev.changed_at

            new_state = MicState(
                state=state,
                muted=muted,
                verified=verified,
                source=source,
                revision=new_revision,
                changed_at=changed_at,
                sampled_at=now_wall,
                sampled_monotonic=now_mono,
                error_code=error_code,
                error_message=error_message,
            )
            self._state = new_state

        if state_changed:
            self._notify_state_changed(new_state)

        return new_state

    def _notify_state_changed(self, state: MicState) -> None:
        callback = self._on_state_changed
        if callback is not None and not self._closed:
            try:
                callback(state)
            except Exception:
                pass

    def _make_result(
        self,
        *,
        ok: bool,
        requested: bool,
        changed: bool,
        default_source_changed: bool | None,
        duration_ms: int,
        state: MicState,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> MicResult:
        return MicResult(
            ok=ok,
            requested_muted=requested,
            changed=changed,
            default_source_changed=default_source_changed,
            duration_ms=max(0, duration_ms),
            state=state,
            error_code=error_code,
            error_message=error_message,
        )

    # ── Background Thread Loop ────────────────────────────────────────────

    def _thread_main(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_step()
            except Exception:
                pass

            # Sleep poll_interval with interruptible wait
            self._stop_event.wait(timeout=self._poll_interval)

    def _poll_step(self) -> None:
        """Run one periodic check: identity (slow) or fast mute read."""
        if self._closed:
            return

        now_mono = self._clock()
        need_identity = now_mono >= self._next_identity_mono

        with self._state_lock:
            current_source = self._state.source

        if need_identity or current_source is None:
            source, err_code, err_msg = self._runner.list_default_source()
            if source is None or err_code is not None:
                self._update_state(
                    state=STATE_UNAVAILABLE,
                    muted=None,
                    verified=False,
                    source=None,
                    error_code=err_code or ERROR_NO_DEFAULT_SOURCE,
                    error_message=err_msg or "Default audio source unavailable",
                )
                self._next_identity_mono = now_mono + self._identity_interval
                return

            current_source = source
            self._next_identity_mono = now_mono + self._identity_interval

        # Fast read on current default source node ID
        reading, err_code, err_msg = self._runner.get_volume(current_source.id)
        if reading is None or err_code is not None:
            self._update_state(
                state=STATE_ERROR,
                muted=None,
                verified=False,
                source=current_source,
                error_code=err_code or ERROR_INTERNAL_ERROR,
                error_message=err_msg or "Failed to read microphone mute state",
            )
            # Force identity re-check on next iteration
            self._next_identity_mono = 0.0
            return

        target_state = STATE_MUTED if reading.muted else STATE_UNMUTED
        self._update_state(
            state=target_state,
            muted=reading.muted,
            verified=True,
            source=current_source,
        )


__all__ = [
    "MicrophoneService",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_IDENTITY_INTERVAL",
    "DEFAULT_STATE_TTL",
    "DEFAULT_RATE_LIMIT",
]
