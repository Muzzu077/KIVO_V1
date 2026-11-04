"""Unit tests for the KIVO conversation subsystem."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from kivo.conversation.context_manager import ContextManager
from kivo.conversation.conversation_config import (
    load_conversation_config,
    save_conversation_config,
)
from kivo.conversation.conversation_manager import ConversationManager
from kivo.conversation.exceptions import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
)
from kivo.conversation.models import (
    Conversation,
    ConversationStatus,
    Message,
    ProviderStatus,
)
from kivo.conversation.prompt_manager import get_system_prompt
from kivo.conversation.providers.base import LLMProvider
from kivo.conversation.providers.openrouter import OpenRouterProvider
from kivo.stt.models import TranscriptionResult


class ConversationModelsTest(unittest.TestCase):
    def test_message_serialization_and_bounding(self):
        long_content = "A" * 20000
        msg = Message(role="user", content=long_content)
        d = msg.to_dict()
        self.assertEqual(d["role"], "user")
        self.assertLessEqual(len(d["content"]), 12000)
        self.assertIn("message_id", d)
        self.assertIn("timestamp", d)

    def test_provider_status_to_dict(self):
        st = ProviderStatus(
            provider_name="openrouter",
            model="test-model",
            cloud_enabled=False,
            available=False,
            error_message="Disabled",
        )
        d = st.to_dict()
        self.assertEqual(d["provider_name"], "openrouter")
        self.assertFalse(d["cloud_enabled"])


class ContextManagerTest(unittest.TestCase):
    def test_multi_turn_context_preservation(self):
        cm = ContextManager(max_messages=10)
        u1 = cm.add_user_message("What is an API?")
        a1 = cm.add_assistant_message("An API connects software.")
        u2 = cm.add_user_message("Give me an example.")

        messages = cm.get_messages_for_llm()
        # Should have system prompt + 3 turns
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["content"], "What is an API?")
        self.assertEqual(messages[2]["content"], "An API connects software.")
        self.assertEqual(messages[3]["content"], "Give me an example.")

    def test_context_budget_truncation(self):
        cm = ContextManager(max_messages=20)
        # Add messages totaling more than budget
        for i in range(15):
            cm.add_user_message(f"User utterance {i}: " + ("x" * 1500))
            cm.add_assistant_message(f"Assistant reply {i}: " + ("y" * 1500))

        messages = cm.get_messages_for_llm()
        self.assertEqual(messages[0]["role"], "system")
        # Total characters must be bounded
        total_chars = sum(len(m["content"]) for m in messages)
        self.assertLessEqual(total_chars, 20000)

    def test_clear_resets_conversation(self):
        cm = ContextManager()
        cm.add_user_message("Hello")
        old_id = cm.conversation_id
        cm.clear()
        self.assertEqual(cm.message_count(), 0)
        self.assertNotEqual(cm.conversation_id, old_id)


class ConversationConfigTest(unittest.TestCase):
    def test_cloud_processing_disabled_by_default(self):
        # Fresh config check
        cfg = load_conversation_config()
        self.assertIn("cloud_enabled", cfg)
        self.assertIn("model", cfg)
        self.assertIn("provider", cfg)

    def test_save_and_restore_config(self):
        updated = save_conversation_config(cloud_enabled=True, model="test/model:free")
        self.assertTrue(updated["cloud_enabled"])
        self.assertEqual(updated["model"], "test/model:free")

        # Restore default to False
        reverted = save_conversation_config(cloud_enabled=False)
        self.assertFalse(reverted["cloud_enabled"])


class OpenRouterProviderTest(unittest.TestCase):
    def test_provider_status_reflects_missing_key_and_consent(self):
        # When cloud is disabled
        provider = OpenRouterProvider(
            api_key_resolver=lambda: "",
            cloud_enabled_getter=lambda: False,
        )
        status = asyncio.run(provider.get_status())
        self.assertFalse(status.cloud_enabled)
        self.assertFalse(status.available)
        self.assertIn("disabled", status.error_message.lower())

    def test_missing_api_key_raises_auth_error(self):
        provider = OpenRouterProvider(
            api_key_resolver=lambda: "",
            cloud_enabled_getter=lambda: True,
        )
        with self.assertRaises(ProviderAuthenticationError):
            provider._build_headers()

    @patch("httpx.AsyncClient.stream")
    def test_streaming_parses_sse_chunks(self, mock_stream):
        # Mock streaming response
        mock_response = MagicMock()
        mock_response.status_code = 200

        async def fake_aiter_lines():
            yield ": openrouter comment"
            yield 'data: {"choices": [{"delta": {"content": "Hello"}}]}'
            yield ""
            yield 'data: {"choices": [{"delta": {"content": " world"}}]}'
            yield "data: [DONE]"

        mock_response.aiter_lines = fake_aiter_lines

        # Set up async context manager mock
        mock_stream.return_value.__aenter__.return_value = mock_response

        provider = OpenRouterProvider(
            api_key_resolver=lambda: "sk-mock-key-12345",
            cloud_enabled_getter=lambda: True,
        )

        async def collect():
            return [chunk async for chunk in provider.stream([{"role": "user", "content": "hi"}])]

        chunks = asyncio.run(collect())
        self.assertEqual(chunks, ["Hello", " world"])


class ConversationManagerTest(unittest.TestCase):
    def setUp(self):
        self.cm = ContextManager()
        self.mock_provider = MagicMock(spec=LLMProvider)

    def tearDown(self):
        pass

    def test_cloud_disabled_prevents_llm_submission(self):
        # Provider must NOT be called when cloud_enabled is False
        user_msgs = []
        statuses = []

        mgr = ConversationManager(
            context_manager=self.cm,
            provider=self.mock_provider,
            on_user_message=user_msgs.append,
            on_state_change=statuses.append,
        )
        mgr._cfg["cloud_enabled"] = False

        submitted = mgr.submit_text("What is quantum computing?")
        self.assertFalse(submitted)
        self.assertEqual(mgr.get_status().state, "cloud_disabled")
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[0].content, "What is quantum computing?")
        self.mock_provider.stream.assert_not_called()
        mgr.close()

    def test_transcription_deduplication(self):
        mgr = ConversationManager(
            context_manager=self.cm,
            provider=self.mock_provider,
        )
        mgr._cfg["cloud_enabled"] = False

        t_result = TranscriptionResult(
            capture_id="cap-same-1",
            text="Hello there",
            status="completed",
        )
        first = mgr.submit_transcript(t_result)
        self.assertFalse(first)  # cloud disabled

        # Resubmitting same capture_id is ignored
        second = mgr.submit_transcript(t_result)
        self.assertFalse(second)
        # Context should only have 1 message
        self.assertEqual(self.cm.message_count(), 1)
        mgr.close()

    def test_cloud_enabled_generates_response(self):
        # Mock streaming chunks
        async def mock_stream(*args, **kwargs):
            yield "Artificial "
            yield "Intelligence."

        self.mock_provider.stream = mock_stream

        completed_msgs = []
        deltas = []

        mgr = ConversationManager(
            context_manager=self.cm,
            provider=self.mock_provider,
            on_response_delta=lambda d, acc: deltas.append(d),
            on_response_completed=completed_msgs.append,
        )
        mgr._cfg["cloud_enabled"] = True

        submitted = mgr.submit_text("Explain AI")
        self.assertTrue(submitted)

        # Allow async generation task to run
        time.sleep(0.5)

        self.assertEqual(deltas, ["Artificial ", "Intelligence."])
        self.assertEqual(len(completed_msgs), 1)
        self.assertEqual(completed_msgs[0].content, "Artificial Intelligence.")
        # Context has user and assistant messages
        self.assertEqual(self.cm.message_count(), 2)

        mgr.close()


if __name__ == "__main__":
    unittest.main()
