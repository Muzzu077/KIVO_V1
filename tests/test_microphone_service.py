"""Service-level and adversarial tests for MicrophoneService.

Exercises resolve -> set -> verify, device change detection, rate limiting,
concurrency, lifecycle, caching, and security boundaries with mocked runners.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from kivo.microphone.models import (
    ERROR_BUSY,
    ERROR_COMMAND_FAILED,
    ERROR_COMMAND_TIMEOUT,
    ERROR_DEVICE_CHANGED,
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
from kivo.microphone.service import MicrophoneService
from kivo.microphone.wireplumber import VolumeReading, WpctlRunner


class _FakeWpctlRunner(WpctlRunner):
    """Deterministic, in-memory fake WpctlRunner for service tests."""

    def __init__(
        self,
        *,
        default_id: int = 84,
        default_name: str = "alsa_input.mic",
        initial_muted: bool = False,
        initial_vol: float = 0.5,
        fail_list: bool = False,
        fail_get: bool = False,
        fail_set: bool = False,
        device_change_on_set: int | None = None,
        get_delay: float = 0.0,
        set_delay: float = 0.0,
    ):
        super().__init__(wpctl_path="/usr/bin/wpctl")
        self.default_id = default_id
        self.default_name = default_name
        self.muted = initial_muted
        self.vol = initial_vol
        self.fail_list = fail_list
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.device_change_on_set = device_change_on_set
        self.get_delay = get_delay
        self.set_delay = set_delay

        self.list_calls = 0
        self.get_calls = 0
        self.set_calls: list[tuple[int, bool]] = []

    def list_default_source(self) -> tuple[MicSource | None, str | None, str | None]:
        self.list_calls += 1
        if self.fail_list:
            return None, ERROR_NO_DEFAULT_SOURCE, "No default source"
        return (
            MicSource(
                id=self.default_id,
                name=self.default_name,
                description="Test Microphone",
                is_default=True,
            ),
            None,
            None,
        )

    def get_volume(
        self, target: str | int
    ) -> tuple[VolumeReading | None, str | None, str | None]:
        self.get_calls += 1
        if self.get_delay > 0:
            time.sleep(self.get_delay)
        if self.fail_get:
            return None, ERROR_COMMAND_FAILED, "Failed to get volume"
        return VolumeReading(volume=self.vol, muted=self.muted), None, None

    def set_mute(
        self, node_id: int, mute: bool
    ) -> tuple[bool, str | None, str | None]:
        self.set_calls.append((node_id, mute))
        if self.set_delay > 0:
            time.sleep(self.set_delay)
        if self.fail_set:
            return False, ERROR_COMMAND_FAILED, "set-mute failed"

        # Apply mute change to internal state
        self.muted = mute

        # Simulate device change on set if requested
        if self.device_change_on_set is not None:
            self.default_id = self.device_change_on_set
            self.default_name = f"alsa_input.new_mic_{self.device_change_on_set}"

        return True, None, None


class MicrophoneOperationsTests(unittest.TestCase):
    # ── Explicit mute ─────────────────────────────────────────────────────
    def test_explicit_mute_success(self):
        runner = _FakeWpctlRunner(initial_muted=False)
        service = MicrophoneService(runner=runner)
        result = service.set_mute(True)
        self.assertTrue(result.ok)
        self.assertTrue(result.requested_muted)
        self.assertTrue(result.changed)
        self.assertFalse(result.default_source_changed)
        self.assertEqual(result.state.state, STATE_MUTED)
        self.assertTrue(result.state.muted)
        self.assertTrue(result.state.verified)
        self.assertEqual(runner.set_calls, [(84, True)])

    # ── Explicit unmute ───────────────────────────────────────────────────
    def test_explicit_unmute_success(self):
        runner = _FakeWpctlRunner(initial_muted=True)
        service = MicrophoneService(runner=runner)
        result = service.set_mute(False)
        self.assertTrue(result.ok)
        self.assertFalse(result.requested_muted)
        self.assertTrue(result.changed)
        self.assertEqual(result.state.state, STATE_UNMUTED)
        self.assertFalse(result.state.muted)
        self.assertTrue(result.state.verified)
        self.assertEqual(runner.set_calls, [(84, False)])

    # ── Already muted request (idempotent, skips command) ──────────────────
    def test_already_muted_skips_command(self):
        runner = _FakeWpctlRunner(initial_muted=True)
        service = MicrophoneService(runner=runner)
        result = service.set_mute(True)
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(len(runner.set_calls), 0)
        self.assertEqual(result.state.state, STATE_MUTED)

    # ── Already unmuted request (idempotent, skips command) ────────────────
    def test_already_unmuted_skips_command(self):
        runner = _FakeWpctlRunner(initial_muted=False)
        service = MicrophoneService(runner=runner)
        result = service.set_mute(False)
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(len(runner.set_calls), 0)
        self.assertEqual(result.state.state, STATE_UNMUTED)

    # ── Verification mismatch ─────────────────────────────────────────────
    def test_verification_mismatch(self):
        class BrokenRunner(_FakeWpctlRunner):
            def set_mute(self, node_id, mute):
                # Acknowledges command but doesn't actually change hardware state
                self.set_calls.append((node_id, mute))
                return True, None, None

        runner = BrokenRunner(initial_muted=False)
        service = MicrophoneService(runner=runner)
        result = service.set_mute(True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_VERIFICATION_FAILED)
        self.assertEqual(result.state.state, STATE_ERROR)

    # ── Default source changes during operation ───────────────────────────
    def test_default_source_changed_during_op(self):
        runner = _FakeWpctlRunner(
            initial_muted=False,
            device_change_on_set=99,
        )
        service = MicrophoneService(runner=runner)
        result = service.set_mute(True)
        self.assertFalse(result.ok)
        self.assertTrue(result.default_source_changed)
        self.assertEqual(result.error_code, ERROR_DEVICE_CHANGED)
        # New default reported in state
        self.assertEqual(result.state.source.id, 99)

    # ── Rate limiting (250 ms) ────────────────────────────────────────────
    def test_rate_limiting(self):
        runner = _FakeWpctlRunner(initial_muted=False)
        service = MicrophoneService(runner=runner, rate_limit=0.5)
        r1 = service.set_mute(True)
        self.assertTrue(r1.ok)
        # Second immediate request must be rate limited
        r2 = service.set_mute(False)
        self.assertFalse(r2.ok)
        self.assertEqual(r2.error_code, ERROR_RATE_LIMITED)

    # ── Lock timeout / busy ───────────────────────────────────────────────
    def test_busy_when_locked(self):
        runner = _FakeWpctlRunner(initial_muted=False)
        service = MicrophoneService(runner=runner)
        # Hold the lock externally
        service._op_lock.acquire()
        try:
            result = service.set_mute(True)
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, ERROR_BUSY)
        finally:
            service._op_lock.release()

    # ── Operation after shutdown ──────────────────────────────────────────
    def test_operation_after_close_rejected(self):
        runner = _FakeWpctlRunner()
        service = MicrophoneService(runner=runner)
        service.close()
        result = service.set_mute(True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, ERROR_SERVICE_UNAVAILABLE)


class MicrophoneLifecycleTests(unittest.TestCase):
    # ── Start and close ───────────────────────────────────────────────────
    def test_start_and_close(self):
        runner = _FakeWpctlRunner()
        service = MicrophoneService(runner=runner)
        self.assertTrue(service.start())
        self.assertTrue(service.is_running)
        time.sleep(0.1)
        service.close(1.0)
        self.assertFalse(service.is_running)

    # ── Repeated start/stop ───────────────────────────────────────────────
    def test_repeated_close_idempotent(self):
        runner = _FakeWpctlRunner()
        service = MicrophoneService(runner=runner)
        service.start()
        service.close(1.0)
        service.close(1.0)  # Must not raise
        self.assertFalse(service.start())  # Start after close rejected

    # ── Thread count cleanup ──────────────────────────────────────────────
    def test_thread_cleaned_up(self):
        before = threading.active_count()
        runner = _FakeWpctlRunner()
        service = MicrophoneService(runner=runner)
        service.start()
        self.assertEqual(service.thread_name, "kivo-microphone")
        time.sleep(0.1)
        service.close(2.0)
        time.sleep(0.1)
        after = threading.active_count()
        self.assertLessEqual(after, before)

    # ── Never unmutes on shutdown ─────────────────────────────────────────
    def test_never_unmutes_on_shutdown(self):
        runner = _FakeWpctlRunner(initial_muted=True)
        service = MicrophoneService(runner=runner)
        service.start()
        time.sleep(0.1)
        service.close(1.0)
        # Verify set_mute was never called by shutdown
        self.assertEqual(len(runner.set_calls), 0)

    # ── State caching TTL ─────────────────────────────────────────────────
    def test_get_state_cache_ttl(self):
        runner = _FakeWpctlRunner(initial_muted=False)
        service = MicrophoneService(runner=runner, state_ttl=2.0)
        s1 = service.get_state()
        calls_after_first = runner.get_calls
        # Immediate second call should use cache
        s2 = service.get_state()
        self.assertEqual(runner.get_calls, calls_after_first)
        self.assertEqual(s1.revision, s2.revision)

    # ── Revision increment on state change ────────────────────────────────
    def test_revision_increments_on_change(self):
        runner = _FakeWpctlRunner(initial_muted=False)
        service = MicrophoneService(runner=runner)
        s0 = service.get_state()
        rev0 = s0.revision

        service.set_mute(True)
        s1 = service.get_state()
        self.assertGreater(s1.revision, rev0)

    # ── on_state_changed callback ─────────────────────────────────────────
    def test_callback_notified_on_state_change(self):
        received: list[MicState] = []
        runner = _FakeWpctlRunner(initial_muted=False)
        service = MicrophoneService(
            on_state_changed=lambda st: received.append(st),
            runner=runner,
        )
        service.set_mute(True)
        self.assertGreaterEqual(len(received), 1)
        self.assertTrue(received[-1].muted)


class MicrophoneSecurityTests(unittest.TestCase):
    """Verify security boundaries and isolation of the microphone package."""

    def test_no_audio_recording_or_capture_imports(self):
        """Microphone service must not import sounddevice, pyaudio, or audio capture."""
        import inspect
        from kivo import microphone
        import kivo.microphone.models as models
        import kivo.microphone.wireplumber as wireplumber
        import kivo.microphone.service as service

        for mod in (models, wireplumber, service):
            source = inspect.getsource(mod)
            for forbidden in (
                "import sounddevice",
                "import pyaudio",
                "import AudioCaptureManager",
                "AudioCaptureManager(",
                "AudioIngress(",
                "pactl record",
                "arecord",
                "pw-record",
            ):
                self.assertNotIn(
                    forbidden,
                    source,
                    f"Microphone module {mod.__name__} must not reference {forbidden}",
                )

    def test_no_agent_tool_registration(self):
        """Default microphone mute must not be registered as an agent tool."""
        import kivo.agent.tools as tools
        tool_names = [name.lower() for name in dir(tools)]
        for forbidden in ("mute", "unmute", "microphone", "mic"):
            for name in tool_names:
                self.assertNotIn(
                    forbidden,
                    name,
                    f"Agent tool module must not export microphone tools: {name}",
                )

    def test_no_arbitrary_shell_in_wireplumber(self):
        """WpctlRunner must not invoke a shell."""
        import inspect
        from kivo.microphone import wireplumber
        source = inspect.getsource(wireplumber)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("subprocess.call", source)


if __name__ == "__main__":
    unittest.main()
