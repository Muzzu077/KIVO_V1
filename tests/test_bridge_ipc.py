"""Integration and adversarial tests for Stage 13.3 Phone Bridge IPC operations and events."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from kivo.bridge.models import BridgeState, BridgeStatus
from kivo.ipc.handlers import IPCHandlers
from kivo.ipc.protocol import (
    EVENT_NAMES,
    PROTOCOL_VERSION,
    REQUEST_OPERATIONS,
)
from kivo.ipc.server import IPCServer
from kivo.runtime_state import RuntimeState


class BridgeIPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp.name) / "kivo.sock"
        self.state = RuntimeState()
        self.drive = MagicMock()
        self.orchestrator = MagicMock()
        self.orchestrator.has_active_tasks.return_value = False
        self.orchestrator.get_current_task.return_value = None
        self.orchestrator.task_history_snapshot.return_value = []

        self.bridge_mock = MagicMock()
        self.status = BridgeStatus(
            state=BridgeState.OFFERING.value,
            lan_ip="192.168.1.7",
            port=8443,
            cert_fingerprint="A" * 64,
            qr_data_url="data:image/png;base64,sample_qr",
        )
        self.bridge_mock.get_status.return_value = self.status
        self.bridge_mock.start_bridge.return_value = {
            "ok": True,
            "status": self.status.to_dict(),
        }
        self.bridge_mock.stop_bridge.return_value = True
        self.bridge_mock.approve_pairing.return_value = True
        self.bridge_mock.reject_pairing.return_value = True
        self.bridge_mock.revoke_device.return_value = True
        self.bridge_mock.get_transfers.return_value = {"ok": True, "transfers": []}
        self.bridge_mock.resolve_transfer.return_value = {"ok": True, "transfer": {"transfer_id": "tr_112233445566778899aabbcc"}}
        self.bridge_mock.cancel_transfer.return_value = {"ok": True, "transfer": {"transfer_id": "tr_112233445566778899aabbcc"}}

        self.handlers = IPCHandlers(
            self.state,
            self.drive,
            self.orchestrator,
            bridge_service=self.bridge_mock,
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
            "id": "req-bridge-1",
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
            "bridge_start", "bridge.start",
            "bridge_stop", "bridge.stop",
            "bridge_get_status", "bridge.get_status",
            "bridge_approve_pairing", "bridge.approve_pairing",
            "bridge_reject_pairing", "bridge.reject_pairing",
            "bridge_revoke_device", "bridge.revoke_device",
            "bridge_get_transfers", "bridge.get_transfers",
            "bridge_resolve_transfer", "bridge.resolve_transfer",
            "bridge_cancel_transfer", "bridge.cancel_transfer",
        ):
            self.assertIn(op, REQUEST_OPERATIONS)
        self.assertIn("bridge_status_changed", EVENT_NAMES)
        self.assertIn("bridge.status_changed", EVENT_NAMES)
        self.assertIn("bridge_pairing_requested", EVENT_NAMES)
        self.assertIn("bridge.pairing_requested", EVENT_NAMES)
        self.assertIn("bridge_transfer_requested", EVENT_NAMES)
        self.assertIn("bridge.transfer_requested", EVENT_NAMES)
        self.assertIn("bridge_transfer_progress", EVENT_NAMES)
        self.assertIn("bridge.transfer_progress", EVENT_NAMES)
        self.assertIn("bridge_transfer_completed", EVENT_NAMES)
        self.assertIn("bridge.transfer_completed", EVENT_NAMES)

    # ── bridge_get_status ──────────────────────────────────────────────────
    async def test_get_status_success(self):
        resp = await self._send("bridge_get_status")
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertEqual(result.get("state"), BridgeState.OFFERING.value)
        self.assertEqual(result.get("lan_ip"), "192.168.1.7")
        self.assertEqual(result.get("port"), 8443)
        self.assertTrue(result.get("qr_data_url").startswith("data:image/png;base64,"))

    async def test_get_status_rejects_extra_params(self):
        resp = await self._send("bridge_get_status", {"extra": True})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── bridge_start ───────────────────────────────────────────────────────
    async def test_start_success(self):
        resp = await self._send("bridge_start", {"port": 8443})
        self.assertTrue(resp.get("ok"))
        self.bridge_mock.start_bridge.assert_called_with(preferred_port=8443)

    async def test_start_rejects_privileged_port(self):
        resp = await self._send("bridge_start", {"port": 80})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── bridge_stop ────────────────────────────────────────────────────────
    async def test_stop_success(self):
        resp = await self._send("bridge_stop")
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.bridge_mock.stop_bridge.assert_called_once()

    # ── bridge_approve_pairing ─────────────────────────────────────────────
    async def test_approve_pairing_success(self):
        resp = await self._send("bridge_approve_pairing", {"session_id": "sess_1234"})
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.bridge_mock.approve_pairing.assert_called_with("sess_1234")

    async def test_approve_pairing_rejects_invalid_id(self):
        for bad_id in ("sess;rm -rf /", "sess\x001", ""):
            resp = await self._send("bridge_approve_pairing", {"session_id": bad_id})
            self.assertFalse(resp.get("ok"))
            self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── bridge_reject_pairing ──────────────────────────────────────────────
    async def test_reject_pairing_success(self):
        resp = await self._send("bridge_reject_pairing", {"session_id": "sess_1234"})
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.bridge_mock.reject_pairing.assert_called_with("sess_1234")

    # ── bridge_revoke_device ───────────────────────────────────────────────
    async def test_revoke_device_success(self):
        resp = await self._send("bridge_revoke_device", {"session_id": "sess_1234"})
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("result", {}).get("ok"))
        self.bridge_mock.revoke_device.assert_called_with("sess_1234")

    # ── bridge_get_transfers ───────────────────────────────────────────────
    async def test_get_transfers_success(self):
        resp = await self._send("bridge_get_transfers")
        self.assertTrue(resp.get("ok"))
        self.assertEqual(resp.get("result", {}).get("transfers"), [])
        self.bridge_mock.get_transfers.assert_called_once()

    async def test_get_transfers_rejects_extra_params(self):
        resp = await self._send("bridge_get_transfers", {"extra": True})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── bridge_resolve_transfer ────────────────────────────────────────────
    async def test_resolve_transfer_allow_receive(self):
        valid_id = "tr_112233445566778899aabbcc"
        resp = await self._send("bridge_resolve_transfer", {
            "transfer_id": valid_id,
            "decision": "allow_receive",
        })
        self.assertTrue(resp.get("ok"))
        self.bridge_mock.resolve_transfer.assert_called_with(valid_id, "allow_receive")

    async def test_resolve_transfer_accept_file(self):
        valid_id = "tr_112233445566778899aabbcc"
        resp = await self._send("bridge_resolve_transfer", {
            "transfer_id": valid_id,
            "decision": "accept_file",
        })
        self.assertTrue(resp.get("ok"))
        self.bridge_mock.resolve_transfer.assert_called_with(valid_id, "accept_file")

    async def test_resolve_transfer_rejects_invalid_decision(self):
        resp = await self._send("bridge_resolve_transfer", {
            "transfer_id": "tr_112233445566778899aabbcc",
            "decision": "force_publish",
        })
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    async def test_resolve_transfer_rejects_malformed_id(self):
        for bad_id in ("tr_short", "tr_UPPERCASE1122334455667788", "not_a_tid", "../escaped"):
            resp = await self._send("bridge_resolve_transfer", {
                "transfer_id": bad_id,
                "decision": "allow_receive",
            })
            self.assertFalse(resp.get("ok"))
            self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── bridge_cancel_transfer ─────────────────────────────────────────────
    async def test_cancel_transfer_success(self):
        valid_id = "tr_112233445566778899aabbcc"
        resp = await self._send("bridge_cancel_transfer", {"transfer_id": valid_id})
        self.assertTrue(resp.get("ok"))
        self.bridge_mock.cancel_transfer.assert_called_with(valid_id)

    async def test_cancel_transfer_rejects_malformed_id(self):
        resp = await self._send("bridge_cancel_transfer", {"transfer_id": "bad_id"})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")

    # ── Event delivery ─────────────────────────────────────────────────────
    async def test_event_delivery(self):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        sub = {
            "version": PROTOCOL_VERSION,
            "type": "request",
            "id": "sub-bridge-1",
            "operation": "subscribe_events",
            "params": {"events": ["bridge_pairing_requested", "bridge_transfer_requested"]},
        }
        writer.write((json.dumps(sub) + "\n").encode("utf-8"))
        await writer.drain()
        sub_resp = json.loads((await reader.readline()).decode("utf-8"))
        self.assertTrue(sub_resp.get("ok"))

        # Publish pairing request event
        self.server.publish("bridge_pairing_requested", {
            "session_id": "sess_1234",
            "device_name": "Pixel 8",
            "sas_code": "482 910",
        })

        # Read event line
        event_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        event_msg = json.loads(event_line.decode("utf-8"))
        self.assertEqual(event_msg.get("type"), "event")
        self.assertEqual(event_msg.get("event"), "bridge_pairing_requested")
        self.assertEqual(event_msg.get("data", {}).get("sas_code"), "482 910")

        # Publish transfer requested event
        self.server.publish("bridge_transfer_requested", {
            "transfer_id": "tr_112233445566778899aabbcc",
            "filename": "document.pdf",
            "size": 1024,
        })
        event_line2 = await asyncio.wait_for(reader.readline(), timeout=2.0)
        event_msg2 = json.loads(event_line2.decode("utf-8"))
        self.assertEqual(event_msg2.get("event"), "bridge_transfer_requested")
        self.assertEqual(event_msg2.get("data", {}).get("filename"), "document.pdf")

        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
