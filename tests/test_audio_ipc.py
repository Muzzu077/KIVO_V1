"""IPC integration tests for KIVO audio commands and events."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kivo.app import KivoApp
from kivo.audio.audio_models import AudioCapture, AudioDevice


class AudioIPCTests(unittest.TestCase):
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

        # Read line response, skipping any asynchronous event lines
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

    def test_audio_get_status(self):
        resp = self._request("audio_get_status")
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertIn("available", result)
        self.assertIn("state", result)
        self.assertIn("capturing", result)
        self.assertFalse(result["capturing"])

    def test_audio_list_devices(self):
        resp = self._request("audio_list_devices")
        self.assertTrue(resp.get("ok"))
        result = resp.get("result", {})
        self.assertIn("devices", result)
        self.assertIsInstance(result["devices"], list)

    def test_audio_select_device_valid_and_invalid(self):
        # Empty string selects system default
        resp = self._request("audio_select_device", {"device": ""})
        self.assertTrue(resp.get("ok"))

        # Invalid device returns error
        resp = self._request("audio_select_device", {"device": "CompletelyFakeMicrophoneName999"})
        self.assertTrue(resp.get("ok"))
        self.assertFalse(resp.get("result", {}).get("ok"))

    def test_audio_capture_lifecycle_via_ipc(self):
        # Subscribe to audio events
        sub_resp = self._request("subscribe_events", {
            "events": [
                "audio_capture_started",
                "audio_capture_stopped",
                "audio_capture_completed",
                "audio_state_changed",
            ]
        })
        self.assertTrue(sub_resp.get("ok"))

        # Start capture
        start_resp = self._request("audio_start_capture")
        self.assertTrue(start_resp.get("ok"))
        self.assertTrue(start_resp.get("result", {}).get("ok"))

        # Check status shows capturing
        status_resp = self._request("audio_get_status")
        self.assertTrue(status_resp.get("result", {}).get("capturing"))

        # Stop capture
        time.sleep(0.3)
        stop_resp = self._request("audio_stop_capture")
        self.assertTrue(stop_resp.get("ok"))
        capture_meta = stop_resp.get("result", {}).get("capture")
        self.assertIsNotNone(capture_meta)
        self.assertIn("duration", capture_meta)
        self.assertIn("capture_id", capture_meta)

        # Status returned to idle
        status_resp2 = self._request("audio_get_status")
        self.assertFalse(status_resp2.get("result", {}).get("capturing"))

    def test_raw_audio_bytes_never_appear_in_ipc_messages(self):
        start_resp = self._request("audio_start_capture")
        time.sleep(0.2)
        stop_resp = self._request("audio_stop_capture")

        # Serialized stop response must not contain raw audio bytes
        raw_json = json.dumps(stop_resp)
        self.assertNotIn("audio_bytes", raw_json)
        self.assertNotIn("pcm_data", raw_json)


if __name__ == "__main__":
    unittest.main()
