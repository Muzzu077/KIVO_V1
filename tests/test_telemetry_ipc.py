"""Integration and adversarial tests for Stage 12.2 Telemetry IPC operations and events."""
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
from kivo.runtime_state import RuntimeState
from kivo.telemetry.models import (
    CpuMetrics,
    CpuUtilization,
    MemoryMetrics,
    STATUS_OK,
    TelemetrySnapshot,
)


class TelemetryIPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp.name) / "kivo.sock"
        self.state = RuntimeState()
        self.drive = MagicMock()
        self.orchestrator = MagicMock()
        self.orchestrator.has_active_tasks.return_value = False
        self.orchestrator.get_current_task.return_value = None
        self.orchestrator.task_history_snapshot.return_value = []

        self.telemetry_mock = MagicMock()
        self.fake_snapshot = TelemetrySnapshot(
            sequence=42,
            sampled_at=1700000000.0,
            sampled_monotonic=100.0,
            cpu=CpuMetrics(
                model="Intel Test CPU",
                logical_cores=8,
                utilization=CpuUtilization(
                    status=STATUS_OK, percent=12.5,
                    sampled_at=1700000000.0, sampled_monotonic=100.0,
                ),
            ),
            memory=MemoryMetrics(
                status=STATUS_OK,
                total_bytes=16000000000,
                used_bytes=8000000000,
                available_bytes=8000000000,
                percent=50.0,
                sampled_at=1700000000.0,
                sampled_monotonic=100.0,
            ),
        )
        self.telemetry_mock.latest_snapshot.return_value = self.fake_snapshot

        self.handlers = IPCHandlers(
            self.state,
            self.drive,
            self.orchestrator,
            telemetry_service=self.telemetry_mock,
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
            "id": "req-telemetry-1",
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
        self.assertIn("telemetry_get_snapshot", REQUEST_OPERATIONS)
        self.assertIn("telemetry.get_snapshot", REQUEST_OPERATIONS)
        self.assertIn("telemetry_snapshot_changed", EVENT_NAMES)
        self.assertIn("telemetry.snapshot_changed", EVENT_NAMES)

    # ── Canonical and dotted operations ────────────────────────────────────
    async def test_get_snapshot_canonical(self):
        resp = await self._send("telemetry_get_snapshot")
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertEqual(result.get("sequence"), 42)
        self.assertEqual(result.get("cpu", {}).get("model"), "Intel Test CPU")
        self.assertEqual(result.get("cpu", {}).get("utilization", {}).get("percent"), 12.5)

    async def test_get_snapshot_dotted(self):
        resp = await self._send("telemetry.get_snapshot")
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertEqual(result.get("sequence"), 42)

    # ── Strict validation: no params accepted ──────────────────────────────
    async def test_get_snapshot_rejects_extra_params(self):
        resp = await self._send("telemetry_get_snapshot", {"force": True})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── Missing telemetry service handling ─────────────────────────────────
    async def test_missing_telemetry_service_returns_unavailable(self):
        handlers = IPCHandlers(
            self.state, self.drive, self.orchestrator, telemetry_service=None
        )
        server = IPCServer(self.socket_path.with_name("kivo2.sock"), handlers=handlers)
        server.start()
        await asyncio.sleep(0.05)
        try:
            reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
            req = {
                "version": PROTOCOL_VERSION,
                "type": "request",
                "id": "req-2",
                "operation": "telemetry_get_snapshot",
                "params": {},
            }
            writer.write((json.dumps(req) + "\n").encode("utf-8"))
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
            resp = json.loads(line.decode("utf-8"))
            self.assertTrue(resp.get("ok"))
            self.assertEqual(resp.get("result", {}).get("service_state"), "degraded")
        finally:
            server.stop()

    # ── Event subscription and delivery ────────────────────────────────────
    async def test_event_delivery(self):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        # Subscribe
        sub = {
            "version": PROTOCOL_VERSION,
            "type": "request",
            "id": "sub-1",
            "operation": "subscribe_events",
            "params": {"events": ["telemetry_snapshot_changed"]},
        }
        writer.write((json.dumps(sub) + "\n").encode("utf-8"))
        await writer.drain()
        sub_resp = json.loads((await reader.readline()).decode("utf-8"))
        self.assertTrue(sub_resp.get("ok"))

        # Publish event
        self.server.publish("telemetry_snapshot_changed", self.fake_snapshot.to_dict())

        # Read event line
        event_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        event_msg = json.loads(event_line.decode("utf-8"))
        self.assertEqual(event_msg.get("type"), "event")
        self.assertEqual(event_msg.get("event"), "telemetry_snapshot_changed")
        self.assertEqual(event_msg.get("data", {}).get("sequence"), 42)

        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
