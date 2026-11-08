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
            "turn_id": "turn-test-1",
        })
        self.assertTrue(resp.get("ok"))
        self.assertFalse(resp.get("result", {}).get("ok"))
        self.assertIn("not found", resp.get("result", {}).get("message").lower())

    def test_resolve_approval_requires_turn_id(self):
        resp = self._request("command_resolve_approval", {
            "token": "fake-token-12345",
            "approved": True,
        })
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "invalid_params")
        self.assertIn("turn_id", resp.get("error", {}).get("message").lower())

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
                turn_id="turn-ipc-flow-1",
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
        self.assertEqual(pending[0]["turn_id"], "turn-ipc-flow-1")

        # Approve via IPC
        resolve_resp = self._request("command_resolve_approval", {
            "token": token,
            "approved": True,
            "turn_id": "turn-ipc-flow-1",
        })
        self.assertTrue(resolve_resp.get("ok"))
        self.assertTrue(resolve_resp.get("result", {}).get("ok"))

        approved, status = future.result(timeout=2.0)
        self.assertTrue(approved)
        self.assertEqual(status, "approved")


    def test_turn_bound_approval_resolution_via_ipc(self):
        conv_mgr = self.app._components.get("conversation_manager")
        self.assertIsNotNone(conv_mgr)
        approval_gate = conv_mgr._approval_gate

        async def make_req():
            return await approval_gate.request_approval(
                tool_call_id="c_ipc_turn",
                tool_name="run_readonly_command",
                command="uname",
                arguments=["-r"],
                working_directory=str(Path.home()),
                reason="turn-bound approval",
                turn_id="turn-ipc-test",
            )

        future = asyncio.run_coroutine_threadsafe(make_req(), conv_mgr._loop)

        pending = []
        for _ in range(100):
            pending_resp = self._request("command_get_pending_approvals")
            pending = pending_resp.get("result", {}).get("pending", [])
            if pending:
                break
            time.sleep(0.01)
        self.assertEqual(len(pending), 1)
        token = pending[0]["token"]
        self.assertEqual(pending[0]["turn_id"], "turn-ipc-test")

        wrong_turn = self._request("command_resolve_approval", {
            "token": token,
            "approved": True,
            "turn_id": "turn-other",
        })
        self.assertTrue(wrong_turn.get("ok"))
        self.assertFalse(wrong_turn.get("result", {}).get("ok"))

        resolve_resp = self._request("command_resolve_approval", {
            "token": token,
            "approved": True,
            "turn_id": "turn-ipc-test",
        })
        self.assertTrue(resolve_resp.get("ok"))
        self.assertTrue(resolve_resp.get("result", {}).get("ok"))

        approved, status = future.result(timeout=2.0)
        self.assertTrue(approved)
        self.assertEqual(status, "approved")

    def test_mutation_approval_requires_exact_tool_call_id_via_ipc(self):
        conv_mgr = self.app._components.get("conversation_manager")
        self.assertIsNotNone(conv_mgr)
        approval_gate = conv_mgr._approval_gate

        async def make_req():
            return await approval_gate.request_approval(
                tool_call_id="call-mutation-1",
                tool_name="create_text_file",
                command="filesystem_create",
                arguments=[],
                working_directory="",
                reason="future mutation approval",
                turn_id="turn-mutation-ipc",
                approval_kind="filesystem_mutation",
                details={
                    "workspace_id": "demo",
                    "relative_path": "docs/notes.txt",
                    "byte_count": 5,
                },
                require_tool_call_id=True,
            )

        future = asyncio.run_coroutine_threadsafe(make_req(), conv_mgr._loop)

        pending = []
        for _ in range(100):
            pending_resp = self._request("command_get_pending_approvals")
            pending = pending_resp.get("result", {}).get("pending", [])
            if pending:
                break
            time.sleep(0.01)
        self.assertEqual(len(pending), 1)
        request = pending[0]
        self.assertTrue(request["requires_tool_call_id"])
        self.assertEqual(request["tool_call_id"], "call-mutation-1")
        self.assertEqual(request["approval_kind"], "filesystem_mutation")

        missing_call = self._request("command_resolve_approval", {
            "token": request["token"],
            "approved": True,
            "turn_id": "turn-mutation-ipc",
        })
        self.assertTrue(missing_call.get("ok"))
        self.assertFalse(missing_call.get("result", {}).get("ok"))
        self.assertIn(
            "tool_call_id",
            missing_call.get("result", {}).get("message", "").lower(),
        )

        wrong_call = self._request("command_resolve_approval", {
            "token": request["token"],
            "approved": True,
            "turn_id": "turn-mutation-ipc",
            "tool_call_id": "call-mutation-other",
        })
        self.assertTrue(wrong_call.get("ok"))
        self.assertFalse(wrong_call.get("result", {}).get("ok"))

        resolve_resp = self._request("command_resolve_approval", {
            "token": request["token"],
            "approved": True,
            "turn_id": "turn-mutation-ipc",
            "tool_call_id": "call-mutation-1",
        })
        self.assertTrue(resolve_resp.get("ok"))
        self.assertTrue(resolve_resp.get("result", {}).get("ok"))

        approved, status = future.result(timeout=2.0)
        self.assertTrue(approved)
        self.assertEqual(status, "approved")


if __name__ == "__main__":
    unittest.main()
