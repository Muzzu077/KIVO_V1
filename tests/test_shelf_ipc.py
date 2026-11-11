"""Integration and adversarial tests for Stage 13.2 File Shelf IPC operations and events."""
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
from kivo.shelf.models import FileIdentity, ShelfItem


class ShelfIPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp.name) / "kivo.sock"
        self.state = RuntimeState()
        self.drive = MagicMock()
        self.orchestrator = MagicMock()
        self.orchestrator.has_active_tasks.return_value = False
        self.orchestrator.get_current_task.return_value = None
        self.orchestrator.task_history_snapshot.return_value = []

        self.shelf_mock = MagicMock()
        ident = FileIdentity(dev=1, ino=2, size=100, mtime_ns=1000, mode=0o100644)
        self.fake_item = ShelfItem(
            id="shelf_abc123",
            name="notes.txt",
            file_type="txt",
            size_bytes=100,
            _canonical_path="/home/user/notes.txt",
            _identity=ident,
        )
        self.shelf_mock.list_items.return_value = [self.fake_item]
        self.shelf_mock.add_items.return_value = ([self.fake_item], [])
        self.shelf_mock.remove_item.return_value = True
        self.shelf_mock.clear.return_value = True
        self.shelf_mock.open_item.return_value = (True, None, None)

        self.handlers = IPCHandlers(
            self.state,
            self.drive,
            self.orchestrator,
            shelf_service=self.shelf_mock,
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
            "id": "req-shelf-1",
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
            "shelf_list_items", "shelf.list_items",
            "shelf_add_uris", "shelf.add_uris",
            "shelf_remove_item", "shelf.remove_item",
            "shelf_clear", "shelf.clear",
            "shelf_open_item", "shelf.open_item",
        ):
            self.assertIn(op, REQUEST_OPERATIONS)
        self.assertIn("shelf_items_changed", EVENT_NAMES)
        self.assertIn("shelf.items_changed", EVENT_NAMES)

    # ── shelf_list_items ───────────────────────────────────────────────────
    async def test_list_items_success(self):
        resp = await self._send("shelf_list_items")
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        items = result.get("items", [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "shelf_abc123")
        self.assertEqual(items[0]["name"], "notes.txt")
        # Invariant: internal path/inode withheld
        self.assertNotIn("canonical_path", items[0])
        self.assertNotIn("ino", items[0])

    async def test_list_items_rejects_extra_params(self):
        resp = await self._send("shelf_list_items", {"extra": True})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── shelf_add_uris ─────────────────────────────────────────────────────
    async def test_add_uris_success(self):
        resp = await self._send(
            "shelf_add_uris", {"uris": ["file:///home/user/notes.txt"]}
        )
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertEqual(len(result.get("added", [])), 1)
        self.shelf_mock.add_items.assert_called_with(["file:///home/user/notes.txt"])

    async def test_add_uris_rejects_non_list(self):
        resp = await self._send("shelf_add_uris", {"uris": "file:///path"})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    async def test_add_uris_rejects_empty_list(self):
        resp = await self._send("shelf_add_uris", {"uris": []})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    async def test_add_uris_rejects_batch_exceeding_20(self):
        too_many = [f"file:///path_{i}.txt" for i in range(25)]
        resp = await self._send("shelf_add_uris", {"uris": too_many})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── shelf_remove_item ──────────────────────────────────────────────────
    async def test_remove_item_success(self):
        resp = await self._send("shelf_remove_item", {"id": "shelf_abc123"})
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.shelf_mock.remove_item.assert_called_with("shelf_abc123")

    async def test_remove_item_rejects_invalid_id(self):
        for bad_id in ("shelf;rm -rf /", "shelf\x001", ""):
            resp = await self._send("shelf_remove_item", {"id": bad_id})
            self.assertFalse(resp.get("ok"), f"Failed to reject id: {bad_id!r}")
            self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── shelf_clear ────────────────────────────────────────────────────────
    async def test_clear_success(self):
        resp = await self._send("shelf_clear")
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.shelf_mock.clear.assert_called_once()

    async def test_clear_rejects_params(self):
        resp = await self._send("shelf_clear", {"force": True})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── shelf_open_item ────────────────────────────────────────────────────
    async def test_open_item_success(self):
        resp = await self._send("shelf_open_item", {"id": "shelf_abc123"})
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.shelf_mock.open_item.assert_called_with("shelf_abc123")

    async def test_open_item_returns_typed_failure(self):
        self.shelf_mock.open_item.return_value = (
            False, "stale_reference", "File replaced on disk"
        )
        resp = await self._send("shelf_open_item", {"id": "shelf_abc123"})
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "stale_reference")

    # ── Event delivery ─────────────────────────────────────────────────────
    async def test_event_delivery(self):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        sub = {
            "version": PROTOCOL_VERSION,
            "type": "request",
            "id": "sub-shelf-1",
            "operation": "subscribe_events",
            "params": {"events": ["shelf_items_changed"]},
        }
        writer.write((json.dumps(sub) + "\n").encode("utf-8"))
        await writer.drain()
        sub_resp = json.loads((await reader.readline()).decode("utf-8"))
        self.assertTrue(sub_resp.get("ok"))

        # Publish event
        self.server.publish("shelf_items_changed", {"count": 3})

        # Read event line
        event_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        event_msg = json.loads(event_line.decode("utf-8"))
        self.assertEqual(event_msg.get("type"), "event")
        self.assertEqual(event_msg.get("event"), "shelf_items_changed")
        self.assertEqual(event_msg.get("data", {}).get("count"), 3)

        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
