"""Integration tests for KIVO STT IPC operations and events."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from kivo.app import KivoApp
from kivo.audio.audio_models import AudioCapture
from kivo.stt.models import TranscriptionResult


class STTIPCTests(unittest.TestCase):
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

        # Read line response, ignoring asynchronous events
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

    def test_stt_get_status_via_ipc(self):
        resp = self._request("stt_get_status")
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertIn("state", result)
        self.assertIn("engine", result)
        self.assertIn("model", result)
        self.assertIn("language", result)
        self.assertTrue(result.get("available"))

    def test_stt_update_config_language(self):
        resp = self._request("stt_update_config", {"language": "hi"})
        self.assertTrue(resp.get("ok"))
        status = resp.get("result", {}).get("status", {})
        self.assertEqual(status.get("language"), "hi")

        # Revert to auto
        resp2 = self._request("stt_update_config", {"language": "auto"})
        self.assertTrue(resp2.get("ok"))

    def test_stt_transcription_pipeline_end_to_end(self):
        # Mock engine's transcribe on the live transcription_manager
        stt_mgr = self.app._components.get("transcription_manager")
        self.assertIsNotNone(stt_mgr)

        mock_result = TranscriptionResult(
            capture_id="cap-mock-123",
            text="Open terminal and list files",
            language="en",
            status="completed",
        )
        stt_mgr._engine = MagicMock()
        stt_mgr._engine.get_model_name.return_value = "base"
        stt_mgr._engine.get_device.return_value = "cpu"
        stt_mgr._engine.transcribe.return_value = mock_result

        # Submit audio capture into AudioIngress
        ingress = self.app._components.get("audio_ingress")
        self.assertIsNotNone(ingress)

        capture = AudioCapture(
            capture_id="cap-mock-123",
            audio_bytes=b"\x00\x01" * 8000,
            duration=1.0,
        )
        ingress.submit(capture)

        # Wait for worker to transcribe
        time.sleep(0.5)

        # Check get_result
        res_resp = self._request("stt_get_result")
        self.assertTrue(res_resp.get("ok"))
        res = res_resp.get("result", {}).get("result")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("text"), "Open terminal and list files")
        self.assertEqual(res.get("capture_id"), "cap-mock-123")

        # Check get_history
        hist_resp = self._request("stt_get_history", {"limit": 5})
        self.assertTrue(hist_resp.get("ok"))
        history = hist_resp.get("result", {}).get("history", [])
        self.assertGreaterEqual(len(history), 1)
        self.assertEqual(history[-1].get("text"), "Open terminal and list files")


if __name__ == "__main__":
    unittest.main()
