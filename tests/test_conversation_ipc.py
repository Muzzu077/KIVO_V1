"""IPC integration tests for KIVO conversation operations and events."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from kivo.app import KivoApp
from kivo.conversation.providers.base import LLMProvider
from kivo.stt.models import TranscriptionResult


class ConversationIPCTests(unittest.TestCase):
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

    def test_conversation_get_status(self):
        resp = self._request("conversation_get_status")
        self.assertTrue(resp.get("ok"))
        res = resp.get("result", {})
        self.assertIn("state", res)
        self.assertIn("cloud_enabled", res)
        self.assertIn("active_conversation_id", res)
        self.assertIn("provider", res)

    def test_conversation_update_config_and_cloud_toggle(self):
        # Explicitly toggle cloud_enabled and model
        resp = self._request("conversation_update_config", {
            "cloud_enabled": True,
            "model": "meta-llama/llama-3.3-70b-instruct:free",
        })
        self.assertTrue(resp.get("ok"))
        res = resp.get("result", {}).get("status", {})
        self.assertTrue(res.get("cloud_enabled"))
        self.assertEqual(res.get("provider", {}).get("model"), "meta-llama/llama-3.3-70b-instruct:free")

        # Revert to False (safe default)
        resp2 = self._request("conversation_update_config", {"cloud_enabled": False})
        self.assertTrue(resp2.get("ok"))
        self.assertFalse(resp2.get("result", {}).get("status", {}).get("cloud_enabled"))

    def test_submit_when_cloud_disabled_returns_cloud_disabled_state(self):
        # Cloud disabled
        self._request("conversation_update_config", {"cloud_enabled": False})

        resp = self._request("conversation_submit", {"text": "Tell me a joke"})
        self.assertTrue(resp.get("ok"))
        status = resp.get("result", {}).get("status", {})
        self.assertEqual(status.get("state"), "cloud_disabled")

        # History should still record the user message
        hist_resp = self._request("conversation_get_history")
        self.assertTrue(hist_resp.get("ok"))
        history = hist_resp.get("result", {}).get("history", [])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "Tell me a joke")

    def test_conversation_clear_resets_history(self):
        self._request("conversation_submit", {"text": "Hello"})
        hist1 = self._request("conversation_get_history")["result"]["history"]
        self.assertGreaterEqual(len(hist1), 1)

        clear_resp = self._request("conversation_clear")
        self.assertTrue(clear_resp.get("ok"))

        hist2 = self._request("conversation_get_history")["result"]["history"]
        self.assertEqual(len(hist2), 0)

    def test_conversation_end_to_end_with_transcription(self):
        conv_mgr = self.app._components.get("conversation_manager")
        self.assertIsNotNone(conv_mgr)

        # Mock the provider stream
        async def fake_stream(*args, **kwargs):
            yield "A neural network "
            yield "learns from data."

        mock_prov = MagicMock(spec=LLMProvider)
        mock_prov.stream = fake_stream
        mock_prov.get_status = AsyncMock(return_value=MagicMock(to_dict=lambda: {}))
        conv_mgr._provider = mock_prov

        # Enable cloud processing
        self._request("conversation_update_config", {"cloud_enabled": True})

        # Simulate completed Stage 7 transcription result
        t_result = TranscriptionResult(
            capture_id="cap-test-e2e-conv",
            text="How does deep learning work?",
            status="completed",
        )

        # Submit via conversation manager
        conv_mgr.submit_transcript(t_result)

        # Wait for async stream generation
        time.sleep(0.5)

        # Check status and history
        hist = self._request("conversation_get_history")["result"]["history"]
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["role"], "user")
        self.assertEqual(hist[0]["content"], "How does deep learning work?")
        self.assertEqual(hist[1]["role"], "assistant")
        self.assertEqual(hist[1]["content"], "A neural network learns from data.")

        # Revert cloud_enabled to False
        self._request("conversation_update_config", {"cloud_enabled": False})

    def test_conversation_list_models_and_test_connection_via_ipc(self):
        conv_mgr = self.app._components.get("conversation_manager")
        self.assertIsNotNone(conv_mgr)

        mock_prov = MagicMock(spec=LLMProvider)

        async def fake_list():
            return ["agy/gemini-3.8-flash-high", "cl/anthropic/claude-opus-4.8"]

        async def fake_test():
            return True, "Connected to OmniRoute (2 models available)"

        mock_prov.list_models = fake_list
        mock_prov.test_connection = fake_test
        mock_prov.get_status = AsyncMock(return_value=MagicMock(to_dict=lambda: {}))
        conv_mgr._provider = mock_prov

        # Test conversation_list_models
        resp = self._request("conversation_list_models")
        self.assertTrue(resp.get("ok"))
        models = resp.get("result", {}).get("models", [])
        self.assertEqual(len(models), 2)
        self.assertIn("agy/gemini-3.8-flash-high", models)

        # Test conversation_test_connection
        test_resp = self._request("conversation_test_connection")
        self.assertTrue(test_resp.get("ok"))
        self.assertTrue(test_resp.get("result", {}).get("ok"))
        self.assertIn("OmniRoute", test_resp.get("result", {}).get("message"))

    @patch("kivo.conversation.conversation_config.save_omniroute_api_key")
    def test_conversation_update_omniroute_config(self, mock_save):
        resp = self._request("conversation_update_config", {
            "provider": "omniroute",
            "omniroute_base_url": "http://127.0.0.1:20128/v1",
            "model": "agy/gemini-3.8-flash-high",
            "api_key": "sk-test-secret-key-12345",
        })
        self.assertTrue(resp.get("ok"))
        mock_save.assert_called_once_with("sk-test-secret-key-12345")
        res = resp.get("result", {}).get("status", {})
        self.assertEqual(res.get("provider", {}).get("provider_name"), "omniroute")
        self.assertEqual(res.get("provider", {}).get("model"), "agy/gemini-3.8-flash-high")
        # Ensure secret key is NEVER reflected in status response
        serialized = json.dumps(resp)
        self.assertNotIn("sk-test-secret-key-12345", serialized)

        # Restore default model
        self._request("conversation_update_config", {"model": "auto", "cloud_enabled": False})


if __name__ == "__main__":
    unittest.main()
