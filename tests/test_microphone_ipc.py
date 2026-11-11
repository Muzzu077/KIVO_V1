"""Integration and adversarial tests for Stage 12.2 Default Microphone Mute IPC operations and events."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from kivo.ipc.handlers import IPCHandlers
from kivo.ipc.protocol import (
    EVENT_NAMES,
    PROTOCOL_VERSION,
    REQUEST_OPERATIONS,
)
from kivo.ipc.server import IPCServer
from kivo.microphone.models import (
    ERROR_BUSY,
    ERROR_DEVICE_CHANGED,
    ERROR_RATE_LIMITED,
    ERROR_SERVICE_UNAVAILABLE,
    STATE_MUTED,
    STATE_UNMUTED,
    MicResult,
    MicSource,
    MicState,
)
from kivo.runtime_state import RuntimeState


class MicrophoneIPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp.name) / "kivo.sock"
        self.state = RuntimeState()
        self.drive = MagicMock()
        self.orchestrator = MagicMock()
        self.orchestrator.has_active_tasks.return_value = False
        self.orchestrator.get_current_task.return_value = None
        self.orchestrator.task_history_snapshot.return_value = []

        self.mic_mock = MagicMock()
        self.source = MicSource(id=84, name="alsa_input.mic", description="Internal Microphone")
        self.fake_state = MicState(
            state=STATE_UNMUTED,
            muted=False,
            verified=True,
            source=self.source,
            revision=5,
            changed_at=1700000000.0,
            sampled_at=1700000000.0,
            sampled_monotonic=100.0,
        )
        self.mic_mock.get_state.return_value = self.fake_state

        def fake_set_mute(desired: bool):
            target_state = STATE_MUTED if desired else STATE_UNMUTED
            st = MicState(
                state=target_state,
                muted=desired,
                verified=True,
                source=self.source,
                revision=6,
                changed_at=1700000001.0,
                sampled_at=1700000001.0,
                sampled_monotonic=101.0,
            )
            return MicResult(
                ok=True,
                requested_muted=desired,
                changed=True,
                default_source_changed=False,
                duration_ms=45,
                state=st,
            )

        self.mic_mock.set_mute.side_effect = fake_set_mute

        self.handlers = IPCHandlers(
            self.state,
            self.drive,
            self.orchestrator,
            microphone_service=self.mic_mock,
        )
        self.server = IPCServer(self.socket_path, handlers=self.handlers)
        self.server.start()
        await asyncio.sleep(0.05)

    async def asyncTearDown(self) -> None:
        self.server.stop()
        self.tmp.cleanup()

    async def _send(self, op: str, params: dict | None = None) -> dict:
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        req = {
            "version": PROTOCOL_VERSION,
            "type": "request",
            "id": "req-mic-1",
            "operation": op,
            "params": params if params is not None else {},
        }
        writer.write((json.dumps(req) + "\n").encode("utf-8"))
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(line.decode("utf-8"))

    # ── Protocol constants ─────────────────────────────────────────────────
    def test_protocol_allowlists(self):
        self.assertIn("mic_get_state", REQUEST_OPERATIONS)
        self.assertIn("mic.get_state", REQUEST_OPERATIONS)
        self.assertIn("mic_set_mute", REQUEST_OPERATIONS)
        self.assertIn("mic.set_mute", REQUEST_OPERATIONS)
        self.assertIn("mic_state_changed", EVENT_NAMES)
        self.assertIn("mic.state_changed", EVENT_NAMES)

    # ── Canonical and dotted operations ────────────────────────────────────
    async def test_get_state_canonical(self):
        resp = await self._send("mic_get_state")
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertEqual(result.get("state"), STATE_UNMUTED)
        self.assertFalse(result.get("muted"))
        self.assertTrue(result.get("verified"))
        self.assertEqual(result.get("source", {}).get("id"), 84)

    async def test_get_state_dotted(self):
        resp = await self._send("mic.get_state")
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertEqual(result.get("state"), STATE_UNMUTED)

    async def test_set_mute_explicit_true(self):
        resp = await self._send("mic_set_mute", {"muted": True})
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("requested_muted"))
        self.assertEqual(result.get("state", {}).get("state"), STATE_MUTED)
        self.assertTrue(result.get("state", {}).get("muted"))

    async def test_set_mute_explicit_false(self):
        resp = await self._send("mic.set_mute", {"muted": False})
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertTrue(result.get("ok"))
        self.assertFalse(result.get("requested_muted"))
        self.assertEqual(result.get("state", {}).get("state"), STATE_UNMUTED)
        self.assertFalse(result.get("state", {}).get("muted"))

    # ── Strict validation ──────────────────────────────────────────────────
    async def test_get_state_rejects_extra_params(self):
        resp = await self._send("mic_get_state", {"force": True})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    async def test_set_mute_rejects_non_boolean(self):
        for bad_val in (1, 0, "true", "false", None, [], {}):
            resp = await self._send("mic_set_mute", {"muted": bad_val})
            self.assertFalse(resp.get("ok"), f"Failed to reject muted={bad_val!r}")
            self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    async def test_set_mute_rejects_missing_param(self):
        resp = await self._send("mic_set_mute", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    async def test_set_mute_rejects_extra_params(self):
        resp = await self._send("mic_set_mute", {"muted": True, "device": "mic84"})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── Missing microphone service handling ────────────────────────────────
    async def test_missing_microphone_service_returns_unavailable(self):
        handlers = IPCHandlers(
            self.state, self.drive, self.orchestrator, microphone_service=None
        )
        server = IPCServer(self.socket_path.with_name("kivo_mic2.sock"), handlers=handlers)
        server.start()
        await asyncio.sleep(0.05)
        try:
            reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
            req = {
                "version": PROTOCOL_VERSION,
                "type": "request",
                "id": "req-m2",
                "operation": "mic_get_state",
                "params": {},
            }
            writer.write((json.dumps(req) + "\n").encode("utf-8"))
            await writer.drain()
            resp = json.loads((await reader.readline()).decode("utf-8"))
            self.assertTrue(resp.get("ok"))
            self.assertEqual(resp.get("result", {}).get("state"), "unavailable")

            # Test set_mute with missing service
            req2 = {
                "version": PROTOCOL_VERSION,
                "type": "request",
                "id": "req-m3",
                "operation": "mic_set_mute",
                "params": {"muted": True},
            }
            writer.write((json.dumps(req2) + "\n").encode("utf-8"))
            await writer.drain()
            resp2 = json.loads((await reader.readline()).decode("utf-8"))
            self.assertTrue(resp2.get("ok"))
            self.assertFalse(resp2.get("result", {}).get("ok"))
            self.assertEqual(resp2.get("result", {}).get("error_code"), "service_unavailable")

            writer.close()
            await writer.wait_closed()
        finally:
            server.stop()

    # ── Event delivery ─────────────────────────────────────────────────────
    async def test_event_delivery(self):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        sub = {
            "version": PROTOCOL_VERSION,
            "type": "request",
            "id": "sub-mic-1",
            "operation": "subscribe_events",
            "params": {"events": ["mic_state_changed"]},
        }
        writer.write((json.dumps(sub) + "\n").encode("utf-8"))
        await writer.drain()
        sub_resp = json.loads((await reader.readline()).decode("utf-8"))
        self.assertTrue(sub_resp.get("ok"))

        # Publish event
        self.server.publish("mic_state_changed", self.fake_state.to_dict())

        # Read event line
        event_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        event_msg = json.loads(event_line.decode("utf-8"))
        self.assertEqual(event_msg.get("type"), "event")
        self.assertEqual(event_msg.get("event"), "mic_state_changed")
        self.assertEqual(event_msg.get("data", {}).get("state"), STATE_UNMUTED)

        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
