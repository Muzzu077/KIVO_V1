"""IPC operation, validation, and event tests for desktop-authorized downloads."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from kivo.ipc.handlers import IPCHandlers
from kivo.ipc.protocol import (
    EVENT_NAME_SET,
    PROTOCOL_VERSION,
    REQUEST_OPERATIONS,
    ProtocolError,
    decode_request,
)
from kivo.ipc.server import IPCServer
from kivo.runtime_state import RuntimeState

TICKET_ID = "tkt_" + "a" * 64


def _request(operation: str, params: dict | None = None, request_id: str = "req-dl-1") -> bytes:
    return json.dumps({
        "version": PROTOCOL_VERSION,
        "type": "request",
        "id": request_id,
        "operation": operation,
        "params": params or {},
    }).encode("utf-8")


class DownloadProtocolTests(unittest.TestCase):
    def test_operations_and_events_are_allowlisted(self):
        for operation in (
            "bridge_create_download_ticket",
            "bridge.create_download_ticket",
            "bridge_get_downloads",
            "bridge.get_downloads",
            "bridge_cancel_download",
            "bridge.cancel_download",
        ):
            self.assertIn(operation, REQUEST_OPERATIONS)
        for event in (
            "bridge_download_requested",
            "bridge_download_progress",
            "bridge_download_completed",
            "bridge.download_requested",
            "bridge.download_progress",
            "bridge.download_completed",
        ):
            self.assertIn(event, EVENT_NAME_SET)

    def test_create_ticket_requires_explicit_authorization(self):
        request = decode_request(_request(
            "bridge_create_download_ticket",
            {"item_id": "shelf_0123456789ab", "authorize": True},
        ))
        self.assertEqual(request.params["authorize"], True)
        for params in (
            {"item_id": "shelf_0123456789ab"},
            {"item_id": "shelf_0123456789ab", "authorize": False},
            {"item_id": "shelf_0123456789ab", "authorize": "true"},
            {"item_id": "../etc/passwd", "authorize": True},
            {"item_id": "shelf_0123456789ab", "authorize": True, "path": "/etc/passwd"},
        ):
            with self.assertRaises(ProtocolError) as ctx:
                decode_request(_request("bridge_create_download_ticket", params))
            self.assertEqual(ctx.exception.code, "invalid_params")

    def test_cancel_download_requires_opaque_ticket_id(self):
        request = decode_request(_request(
            "bridge_cancel_download", {"download_id": TICKET_ID}
        ))
        self.assertEqual(request.params["download_id"], TICKET_ID)
        for params in (
            {},
            {"download_id": "tr_" + "a" * 24},
            {"download_id": "tkt_short"},
            {"download_id": TICKET_ID, "item_id": "shelf_0123456789ab"},
        ):
            with self.assertRaises(ProtocolError) as ctx:
                decode_request(_request("bridge_cancel_download", params))
            self.assertEqual(ctx.exception.code, "invalid_params")

    def test_get_downloads_accepts_no_parameters(self):
        request = decode_request(_request("bridge_get_downloads"))
        self.assertEqual(request.params, {})
        with self.assertRaises(ProtocolError):
            decode_request(_request("bridge_get_downloads", {"item_id": "shelf_x"}))


class DownloadIPCHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp.name) / "kivo.sock"
        self.state = RuntimeState()
        self.drive = MagicMock()
        self.orchestrator = MagicMock()
        self.orchestrator.has_active_tasks.return_value = False
        self.orchestrator.get_current_task.return_value = None
        self.orchestrator.task_history_snapshot.return_value = []

        self.bridge = MagicMock()
        self.bridge.create_download_ticket.return_value = {
            "ok": True,
            "download": {
                "download_id": TICKET_ID,
                "filename": "report.pdf",
                "state": "issued",
            },
        }
        self.bridge.get_downloads.return_value = {
            "ok": True,
            "downloads": [{"download_id": TICKET_ID, "state": "issued"}],
        }
        self.bridge.cancel_download.return_value = {
            "ok": True,
            "download": {"download_id": TICKET_ID, "state": "cancelled"},
        }

        self.handlers = IPCHandlers(
            self.state,
            self.drive,
            self.orchestrator,
            bridge_service=self.bridge,
        )
        self.server = IPCServer(self.socket_path, handlers=self.handlers)
        self.server.start()
        await asyncio.sleep(0.05)

    async def asyncTearDown(self) -> None:
        self.server.stop()
        self.tmp.cleanup()

    async def _send(self, operation: str, params: dict | None = None) -> dict:
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        writer.write(_request(operation, params) + b"\n")
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(line.decode("utf-8"))

    async def test_create_ticket_dispatches_explicit_authorization(self):
        response = await self._send(
            "bridge_create_download_ticket",
            {"item_id": "shelf_0123456789ab", "authorize": True},
        )
        self.assertTrue(response.get("ok"))
        self.bridge.create_download_ticket.assert_called_once_with(
            "shelf_0123456789ab", True
        )
        self.assertEqual(
            response["result"]["download"]["download_id"], TICKET_ID
        )

    async def test_get_downloads_returns_snapshot(self):
        response = await self._send("bridge_get_downloads")
        self.assertTrue(response.get("ok"))
        self.assertEqual(len(response["result"]["downloads"]), 1)
        self.bridge.get_downloads.assert_called_once()

    async def test_cancel_download_dispatches_opaque_id(self):
        response = await self._send(
            "bridge_cancel_download", {"download_id": TICKET_ID}
        )
        self.assertTrue(response.get("ok"))
        self.bridge.cancel_download.assert_called_once_with(TICKET_ID)

    async def test_event_delivery(self):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        subscription = {
            "version": PROTOCOL_VERSION,
            "type": "request",
            "id": "sub-dl-1",
            "operation": "subscribe_events",
            "params": {"events": ["bridge_download_progress"]},
        }
        writer.write((json.dumps(subscription) + "\n").encode("utf-8"))
        await writer.drain()
        subscribed = json.loads((await reader.readline()).decode("utf-8"))
        self.assertTrue(subscribed.get("ok"))

        self.server.publish(
            "bridge_download_progress",
            {"download_id": TICKET_ID, "state": "streaming", "bytes_sent": 1024},
        )
        event = json.loads(
            (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode("utf-8")
        )
        self.assertEqual(event.get("event"), "bridge_download_progress")
        self.assertEqual(event["data"]["bytes_sent"], 1024)
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
