"""Adversarial and security regression suite for Stage 12.2.

Verifies strict security boundaries, absence of agent tools, absence of shell
execution, PTT isolation, and protocol fuzzing/rejection behavior.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from kivo.ipc.handlers import IPCHandlers
from kivo.ipc.protocol import PROTOCOL_VERSION, ProtocolError, decode_request
from kivo.ipc.server import IPCServer
from kivo.runtime_state import RuntimeState


class Stage12SecurityRegressionTests(unittest.TestCase):
    """Verify security invariant boundaries remain unbroken."""

    def test_no_microphone_agent_tools(self):
        """Microphone controls must not be registered as agent tools."""
        import kivo.agent.tools as tools
        tool_names = [name.lower() for name in dir(tools)]
        for forbidden in ("mute", "unmute", "microphone", "mic"):
            for name in tool_names:
                self.assertNotIn(
                    forbidden,
                    name,
                    f"Agent tool module must not export microphone tool: {name}",
                )

    def test_no_telemetry_agent_tools(self):
        """Telemetry must not be registered as an agent tool in this stage."""
        import kivo.agent.tools as tools
        tool_names = [name.lower() for name in dir(tools)]
        for forbidden in ("telemetry", "get_telemetry", "system_telemetry"):
            self.assertNotIn(forbidden, tool_names)

    def test_no_clipboard_agent_tools(self):
        """Clipboard operations must not be registered as agent tools."""
        import kivo.agent.tools as tools
        tool_names = [name.lower() for name in dir(tools)]
        for forbidden in ("clipboard", "clip_get", "clip_copy", "clip_history"):
            self.assertNotIn(forbidden, tool_names)

    def test_no_shelf_agent_tools(self):
        """File shelf operations must not be registered as agent tools."""
        import kivo.agent.tools as tools
        tool_names = [name.lower() for name in dir(tools)]
        for forbidden in ("shelf", "file_shelf", "shelf_add", "shelf_open"):
            self.assertNotIn(forbidden, tool_names)

    def test_no_bridge_agent_tools(self):
        """Phone bridge operations must not be registered as agent tools."""
        import kivo.agent.tools as tools
        tool_names = [name.lower() for name in dir(tools)]
        for forbidden in ("bridge", "phone_bridge", "pair_phone", "bridge_start"):
            self.assertNotIn(forbidden, tool_names)

    def test_no_shell_execution_in_new_packages(self):
        """No module in core/bounded_process, telemetry, microphone, clipboard, shelf, or bridge may use shell=True or os.system."""
        import kivo.core.bounded_process as bp
        import kivo.microphone.wireplumber as wp
        import kivo.microphone.service as ms
        import kivo.telemetry.gpu as tg
        import kivo.telemetry.readers as tr
        import kivo.clipboard.runner as cr
        import kivo.clipboard.service as cs
        import kivo.shelf.path_policy as sp
        import kivo.shelf.service as ss
        import kivo.bridge.crypto as bc
        import kivo.bridge.pairing as bpair
        import kivo.bridge.server as bserv
        import kivo.bridge.service as bsvc

        for mod in (bp, wp, ms, tg, tr, cr, cs, sp, ss, bc, bpair, bserv, bsvc):
            source = inspect.getsource(mod)
            self.assertNotIn("shell=True", source, f"{mod.__name__} must not use shell=True")
            self.assertNotIn("os.system", source, f"{mod.__name__} must not use os.system")
            self.assertNotIn("subprocess.call", source, f"{mod.__name__} must not use subprocess.call")

    def test_no_audio_capture_in_microphone_package(self):
        """Microphone package must not import sounddevice or audio capture tools."""
        import kivo.microphone.models as mm
        import kivo.microphone.wireplumber as mw
        import kivo.microphone.service as ms

        for mod in (mm, mw, ms):
            source = inspect.getsource(mod)
            self.assertNotIn("import sounddevice", source)
            self.assertNotIn("AudioCaptureManager", source)
            self.assertNotIn("pw-record", source)
            self.assertNotIn("arecord", source)

    def test_never_unmute_on_shutdown(self):
        """MicrophoneService.close() must never execute set-mute or toggle."""
        from kivo.microphone.service import MicrophoneService
        runner = MagicMock()
        runner.list_default_source.return_value = (None, "no_default_source", "none")
        service = MicrophoneService(runner=runner)
        service.start()
        service.close(1.0)
        runner.set_mute.assert_not_called()

    def test_no_node_id_injection_via_protocol(self):
        """decode_request must strictly reject any node ID or device parameters for mic_set_mute."""
        for injection in (
            {"muted": True, "node_id": 84},
            {"muted": True, "device": "mic"},
            {"muted": True, "command": "reboot"},
            {"muted": True, "id": 1},
        ):
            body = json.dumps({
                "version": 1,
                "type": "request",
                "id": "inj-1",
                "operation": "mic_set_mute",
                "params": injection,
            }).encode("utf-8")
            with self.assertRaises(ProtocolError) as raised:
                decode_request(body)
            self.assertEqual(raised.exception.code, "invalid_params")

    def test_mic_set_mute_strictly_boolean(self):
        """decode_request rejects anything that is not a strict JSON boolean."""
        for bad_val in (1, 0, "true", "false", None, 1.0, [], {}):
            body = json.dumps({
                "version": 1,
                "type": "request",
                "id": "bool-1",
                "operation": "mic_set_mute",
                "params": {"muted": bad_val},
            }).encode("utf-8")
            with self.assertRaises(ProtocolError) as raised:
                decode_request(body)
            self.assertEqual(raised.exception.code, "invalid_params")


class ProtocolFuzzingTests(unittest.IsolatedAsyncioTestCase):
    """Protocol-level adversarial and malformed payload tests."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp.name) / "kivo.sock"
        self.state = RuntimeState()
        self.drive = MagicMock()
        self.orchestrator = MagicMock()
        self.handlers = IPCHandlers(self.state, self.drive, self.orchestrator)
        self.server = IPCServer(self.socket_path, handlers=self.handlers)
        self.server.start()
        await asyncio.sleep(0.05)

    async def asyncTearDown(self) -> None:
        self.server.stop()
        self.tmp.cleanup()

    async def _send_raw(self, line: str) -> dict:
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        writer.write(line.encode("utf-8") + b"\n")
        await writer.drain()
        resp_line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(resp_line.decode("utf-8"))

    async def test_oversized_payload_rejected(self):
        """Messages exceeding MAX_MESSAGE_BYTES (64 KiB) are rejected."""
        huge_str = "A" * 70000
        req = json.dumps({
            "version": 1,
            "type": "request",
            "id": "huge",
            "operation": "get_status",
            "params": {"extra": huge_str},
        })
        resp = await self._send_raw(req)
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "message_too_large")

    async def test_unknown_operation_rejected(self):
        req = json.dumps({
            "version": 1,
            "type": "request",
            "id": "unknown-1",
            "operation": "mic_toggle_mute",
            "params": {},
        })
        resp = await self._send_raw(req)
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("error", {}).get("code"), "unsupported_operation")


if __name__ == "__main__":
    unittest.main()
