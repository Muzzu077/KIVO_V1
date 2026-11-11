"""Integration and adversarial tests for Stage 13.1 Clipboard IPC operations and events."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from kivo.clipboard.models import ClipDetail, ClipItem
from kivo.ipc.handlers import IPCHandlers
from kivo.ipc.protocol import (
    EVENT_NAMES,
    PROTOCOL_VERSION,
    REQUEST_OPERATIONS,
)
from kivo.ipc.server import IPCServer
from kivo.runtime_state import RuntimeState


class ClipboardIPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp.name) / "kivo.sock"
        self.state = RuntimeState()
        self.drive = MagicMock()
        self.orchestrator = MagicMock()
        self.orchestrator.has_active_tasks.return_value = False
        self.orchestrator.get_current_task.return_value = None
        self.orchestrator.task_history_snapshot.return_value = []

        self.clip_mock = MagicMock()
        self.fake_item = ClipItem(
            id="clip_1",
            raw_id="1",
            preview="Sample clipboard text",
            char_count=21,
            size_bytes=21,
        )
        self.sensitive_item = ClipItem(
            id="clip_2",
            raw_id="2",
            preview="••••••••••••",
            is_sensitive=True,
        )
        self.clip_mock.get_history.return_value = [self.fake_item, self.sensitive_item]
        self.clip_mock.get_item.return_value = ClipDetail(
            item=self.fake_item, text_content="Sample clipboard text"
        )
        self.clip_mock.copy_item.return_value = True
        self.clip_mock.pin_item.return_value = True
        self.clip_mock.delete_item.return_value = True
        self.clip_mock.clear_history.return_value = True

        self.handlers = IPCHandlers(
            self.state,
            self.drive,
            self.orchestrator,
            clipboard_service=self.clip_mock,
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
            "id": "req-clip-1",
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
        for op in (
            "clip_get_history", "clip.get_history",
            "clip_get_item", "clip.get_item",
            "clip_copy_item", "clip.copy_item",
            "clip_pin_item", "clip.pin_item",
            "clip_delete_item", "clip.delete_item",
            "clip_clear_history", "clip.clear_history",
        ):
            self.assertIn(op, REQUEST_OPERATIONS)
        self.assertIn("clip_history_changed", EVENT_NAMES)
        self.assertIn("clip.history_changed", EVENT_NAMES)

    # ── get_history ────────────────────────────────────────────────────────
    async def test_get_history_success(self):
        resp = await self._send("clip_get_history")
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertEqual(result.get("total_count"), 2)
        items = result.get("items", [])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["preview"], "Sample clipboard text")
        self.assertEqual(items[1]["preview"], "••••••••••••")

    async def test_get_history_dotted(self):
        resp = await self._send("clip.get_history", {"limit": 10})
        self.assertTrue(resp.get("ok"))

    async def test_get_history_rejects_invalid_filter(self):
        resp = await self._send("clip_get_history", {"filter": "invalid_filter_type"})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    async def test_get_history_rejects_oversized_search(self):
        resp = await self._send("clip_get_history", {"search": "A" * 200})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── get_item ───────────────────────────────────────────────────────────
    async def test_get_item_success(self):
        resp = await self._send("clip_get_item", {"id": "clip_1"})
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("item", {}).get("id"), "clip_1")

    async def test_get_item_rejects_missing_id(self):
        resp = await self._send("clip_get_item", {})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    async def test_get_item_rejects_invalid_id_characters(self):
        for bad_id in ("clip;rm -rf /", "clip\x001", "clip 2", ""):
            resp = await self._send("clip_get_item", {"id": bad_id})
            self.assertFalse(resp.get("ok"), f"Failed to reject id: {bad_id!r}")
            self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── copy_item ──────────────────────────────────────────────────────────
    async def test_copy_item_success(self):
        resp = await self._send("clip_copy_item", {"id": "clip_1"})
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.clip_mock.copy_item.assert_called_with("clip_1")

    # ── pin_item ───────────────────────────────────────────────────────────
    async def test_pin_item_success(self):
        resp = await self._send("clip_pin_item", {"id": "clip_1", "pin": True})
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.clip_mock.pin_item.assert_called_with("clip_1", pin=True)

    async def test_pin_item_rejects_non_bool_pin(self):
        resp = await self._send("clip_pin_item", {"id": "clip_1", "pin": "true"})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── delete_item ────────────────────────────────────────────────────────
    async def test_delete_item_success(self):
        resp = await self._send("clip_delete_item", {"id": "clip_1"})
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.clip_mock.delete_item.assert_called_with("clip_1")

    # ── clear_history ──────────────────────────────────────────────────────
    async def test_clear_history_success(self):
        resp = await self._send("clip_clear_history")
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.clip_mock.clear_history.assert_called_once()

    async def test_clear_history_rejects_params(self):
        resp = await self._send("clip_clear_history", {"force": True})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── Event delivery ─────────────────────────────────────────────────────
    async def test_event_delivery(self):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        sub = {
            "version": PROTOCOL_VERSION,
            "type": "request",
            "id": "sub-clip-1",
            "operation": "subscribe_events",
            "params": {"events": ["clip_history_changed"]},
        }
        writer.write((json.dumps(sub) + "\n").encode("utf-8"))
        await writer.drain()
        sub_resp = json.loads((await reader.readline()).decode("utf-8"))
        self.assertTrue(sub_resp.get("ok"))

        # Publish event
        self.server.publish("clip_history_changed", {"count": 42})

        # Read event line
        event_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        event_msg = json.loads(event_line.decode("utf-8"))
        self.assertEqual(event_msg.get("type"), "event")
        self.assertEqual(event_msg.get("event"), "clip_history_changed")
        self.assertEqual(event_msg.get("data", {}).get("count"), 42)

        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
