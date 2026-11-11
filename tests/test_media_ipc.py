"""Integration tests for Stage 12.1 MPRIS IPC operations and events."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from kivo.ipc.handlers import IPCHandlers
from kivo.ipc.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    REQUEST_OPERATIONS,
    EVENT_NAMES,
)
from kivo.ipc.server import IPCServer
from kivo.media.service import MediaService, MediaSnapshot
from kivo.runtime_state import RuntimeState


class MediaIPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp.name) / "kivo.sock"
        self.state = RuntimeState()
        self.drive = MagicMock()
        self.drive.has_active_session.return_value = False
        self.drive.active_session = None
        self.orchestrator = MagicMock()
        self.orchestrator.has_active_tasks.return_value = False
        self.orchestrator.get_current_task.return_value = None
        self.orchestrator.task_history_snapshot.return_value = []

        self.media_service = MediaService()
        self.handlers = IPCHandlers(
            self.state,
            self.drive,
            self.orchestrator,
            media_service=self.media_service,
        )
        self.server = IPCServer(self.socket_path, handlers=self.handlers)
        self.server.start()
        await asyncio.sleep(0.05)

    async def asyncTearDown(self) -> None:
        self.server.stop()
        await self.media_service.close()
        self.tmp.cleanup()

    async def _send(self, op: str, params: dict | None = None) -> dict:
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        req = {
            "version": PROTOCOL_VERSION,
            "type": "request",
            "id": "test-req-1",
            "operation": op,
            "params": params or {},
        }
        writer.write((json.dumps(req) + "\n").encode("utf-8"))
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(line.decode("utf-8"))

    def test_protocol_constants_include_media_operations(self):
        for op in (
            "media_get_status",
            "media_play",
            "media_pause",
            "media_play_pause",
            "media_next",
            "media_previous",
            "media_seek",
            "media_get_lyrics",
        ):
            self.assertIn(op, REQUEST_OPERATIONS)
        self.assertIn("media_status_changed", EVENT_NAMES)
        self.assertIn("media_lyrics_loaded", EVENT_NAMES)

    async def test_media_get_status_operation(self):
        snap = MediaSnapshot(
            available=True,
            player_name="spotify",
            players=["spotify"],
            status="Playing",
            title="Yellow",
            artist="Coldplay",
            album="Parachutes",
            duration_seconds=267.0,
            position_seconds=42.0,
        )
        self.media_service._snapshot = snap
        self.media_service._position_reference = 42.0
        self.media_service._position_updated_at = 1000.0

        resp = await self._send("media_get_status")
        self.assertTrue(resp.get("ok"), resp)
        res = resp["result"]
        self.assertTrue(res["available"])
        self.assertEqual(res["player_name"], "spotify")
        self.assertEqual(res["title"], "Yellow")
        self.assertEqual(res["artist"], "Coldplay")
        self.assertEqual(res["status"], "Playing")

    async def test_media_control_operations(self):
        with patch.object(self.media_service, "play_pause_sync") as mock_pp, \
             patch.object(self.media_service, "next_sync") as mock_next, \
             patch.object(self.media_service, "previous_sync") as mock_prev, \
             patch.object(self.media_service, "seek_sync") as mock_seek:
            mock_pp.return_value = {"status": "Paused"}
            mock_next.return_value = {"success": True}
            mock_prev.return_value = {"success": True}
            mock_seek.return_value = {"position_seconds": 90.0}

            resp = await self._send("media_play_pause")
            self.assertTrue(resp.get("ok"))
            mock_pp.assert_called_once()

            resp = await self._send("media_next")
            self.assertTrue(resp.get("ok"))
            mock_next.assert_called_once()

            resp = await self._send("media_previous")
            self.assertTrue(resp.get("ok"))
            mock_prev.assert_called_once()

            resp = await self._send("media_seek", {"position_seconds": 90.0})
            self.assertTrue(resp.get("ok"))
            mock_seek.assert_called_once_with(90.0)

    async def test_media_seek_rejects_non_finite_and_huge_values(self):
        for value in (float("inf"), float("nan"), 10**1000):
            response = await self._send(
                "media_seek", {"position_seconds": value}
            )
            self.assertFalse(response.get("ok"), (value, response))

    async def test_media_event_delivery(self):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        # Subscribe
        sub_req = {
            "version": PROTOCOL_VERSION,
            "type": "request",
            "id": "sub-1",
            "operation": "subscribe_events",
            "params": {"events": ["media_status_changed"]},
        }
        writer.write((json.dumps(sub_req) + "\n").encode("utf-8"))
        await writer.drain()
        sub_resp = json.loads((await reader.readline()).decode("utf-8"))
        self.assertTrue(sub_resp["ok"])

        # Broadcast media status event
        snap = MediaSnapshot(
            available=True,
            player_name="vlc",
            status="Playing",
            title="Track Title",
            artist="Artist",
        )
        self.server.publish("media_status_changed", snap.to_dict())

        event_line = await asyncio.wait_for(reader.readline(), timeout=1.0)
        event_obj = json.loads(event_line.decode("utf-8"))
        self.assertEqual(event_obj["type"], "event")
        self.assertEqual(event_obj["event"], "media_status_changed")
        self.assertEqual(event_obj["data"]["player_name"], "vlc")
        self.assertEqual(event_obj["data"]["title"], "Track Title")

        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
