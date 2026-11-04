"""IPC integration tests for terminal command approval operations and events."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

from kivo.app import KivoApp


class TerminalIPCTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        os.chmod(self.tmp_dir.name, 0o700)
        self.socket_path = Path(self.tmp_dir.name) / "kivo.sock"
        self.app = KivoApp(socket_path=self.socket_path)
        self.assertTrue(self.app.initialize())

        # Connect client socket
        self.client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.client.connect(str(self.socket_path))

    def tearDown(self):
        try:
            self.client.close()
        except Exception:
            pass
        self.app.shutdown()
        self.tmp_dir.cleanup()

    def _request(self, operation: str, params: dict | None = None) -> dict:
        req_id = f"req-{time.time()}"
        req = {
            "version": 1,
            "type": "request",
            "id": req_id,
            "operation": operation,
        }
        if params is not None:
            req["params"] = params
        payload = json.dumps(req).encode("utf-8") + b"\n"
        self.client.sendall(payload)

        buffer = b""
        while True:
            while b"\n" in buffer:
                line_bytes, buffer = buffer.split(b"\n", 1)
                line = line_bytes.decode("utf-8").strip()
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("type") == "response" and msg.get("id") == req_id:
                    return msg
            chunk = self.client.recv(4096)
            if not chunk:
                break
            buffer += chunk
        raise AssertionError("Connection closed before response was received")

    def test_get_pending_approvals_initially_empty(self):
        resp = self._request("command_get_pending_approvals")
        self.assertTrue(resp.get("ok"))
        self.assertEqual(resp.get("result", {}).get("pending"), [])

    def test_resolve_approval_invalid_token(self):
        resp = self._request("command_resolve_approval", {
            "token": "fake-token-12345",
            "approved": True,
        })
        self.assertTrue(resp.get("ok"))
        self.assertFalse(resp.get("result", {}).get("ok"))
        self.assertIn("not found", resp.get("result", {}).get("message").lower())

    def test_approval_flow_via_ipc(self):
        conv_mgr = self.app._components.get("conversation_manager")
        self.assertIsNotNone(conv_mgr)

        approval_gate = conv_mgr._approval_gate

        # Create asynchronous approval request task
        async def make_req():
            return await approval_gate.request_approval(
                tool_call_id="c_ipc_1",
                tool_name="run_readonly_command",
                command="uname",
                arguments=["-r"],
                working_directory=str(Path.home()),
                reason="checking kernel release",
            )

        future = asyncio.run_coroutine_threadsafe(make_req(), conv_mgr._loop)
        time.sleep(0.1)

        # Query pending approvals via IPC
        pending_resp = self._request("command_get_pending_approvals")
        self.assertTrue(pending_resp.get("ok"))
        pending = pending_resp.get("result", {}).get("pending", [])
        self.assertEqual(len(pending), 1)
        token = pending[0]["token"]
        self.assertEqual(pending[0]["command"], "uname")

        # Approve via IPC
        resolve_resp = self._request("command_resolve_approval", {
            "token": token,
            "approved": True,
        })
        self.assertTrue(resolve_resp.get("ok"))
        self.assertTrue(resolve_resp.get("result", {}).get("ok"))

        approved, status = future.result(timeout=2.0)
        self.assertTrue(approved)
        self.assertEqual(status, "approved")


if __name__ == "__main__":
    unittest.main()
