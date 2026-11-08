"""Conversation turn isolation: one active turn, bound approvals, clean timeout."""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
from typing import AsyncIterator, Optional

from kivo.agent.tool_registry import ToolRegistry
from kivo.conversation.context_manager import ContextManager
from kivo.conversation.conversation_manager import ConversationManager
from kivo.conversation.models import ProviderStatus
from kivo.conversation.providers.base import LLMProvider


class ControlledProvider(LLMProvider):
    """Provider whose first response can be held open for concurrency tests."""

    def __init__(self) -> None:
        self.block = True
        self.started = threading.Event()

    async def generate(self, messages, tools=None, **kwargs):
        chunks = []
        async for chunk in self.stream(messages, tools=tools, **kwargs):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks)

    async def stream(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        **kwargs,
    ) -> AsyncIterator[str]:
        self.started.set()
        if self.block:
            await asyncio.sleep(5.0)
        yield "completed response"

    async def get_status(self) -> ProviderStatus:
        return ProviderStatus(provider_name="test", available=True)

    async def close(self) -> None:
        return None


class ConversationTurnIsolationTest(unittest.TestCase):
    def setUp(self):
        self.context = ContextManager()
        self.provider = ControlledProvider()
        self.manager = ConversationManager(
            context_manager=self.context,
            provider=self.provider,
            tool_registry=ToolRegistry(),
            turn_timeout_seconds=2.0,
        )
        self.manager._cfg["cloud_enabled"] = True

    def tearDown(self):
        self.manager.close()

    def _wait_for_state(self, states: tuple[str, ...], timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.manager.get_status().state in states:
                return
            time.sleep(0.01)
        self.fail(f"state never became one of {states}: {self.manager.get_status().state}")

    def test_second_turn_is_rejected_while_first_is_active(self):
        self.assertTrue(self.manager.submit_text("first request"))
        self.assertTrue(self.provider.started.wait(timeout=1.0))

        first_turn = self.manager.get_active_turn_id()
        self.assertTrue(first_turn)

        self.assertFalse(self.manager.submit_text("second request"))
        self.assertEqual(self.context.message_count(), 1)

        self.assertTrue(self.manager.cancel_active())
        self._wait_for_state(("cancelled",))
        self.assertIsNone(self.manager.get_active_turn_id())

        # A new turn can start after cancellation, and gets a fresh turn ID.
        self.provider.block = False
        self.assertTrue(self.manager.submit_text("third request"))
        self._wait_for_state(("completed", "idle"))
        self.assertNotEqual(self.manager.get_active_turn_id(), first_turn)
        self.assertEqual(self.context.message_count(), 3)

    def test_cancel_emits_cancelled_callback_once(self):
        cancelled = []
        self.manager._on_cancelled = lambda: cancelled.append(True)

        self.assertTrue(self.manager.submit_text("first request"))
        self.assertTrue(self.provider.started.wait(timeout=1.0))

        self.assertTrue(self.manager.cancel_active())
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(self.manager.get_status().state, "cancelled")
        self.assertIsNone(self.manager.get_active_turn_id())

        self.assertFalse(self.manager.cancel_active())
        self.assertEqual(len(cancelled), 1)

    def test_turn_timeout_is_explicit_and_clears_active_turn(self):
        self.manager.close()

        self.manager = ConversationManager(
            context_manager=self.context,
            provider=self.provider,
            tool_registry=ToolRegistry(),
            turn_timeout_seconds=0.1,
        )
        self.manager._cfg["cloud_enabled"] = True

        self.assertTrue(self.manager.submit_text("slow request"))
        self._wait_for_state(("failed",), timeout=2.0)
        status = self.manager.get_status()
        self.assertIn("timed out", status.error_message.lower())
        self.assertIsNone(self.manager.get_active_turn_id())

        self.provider.block = False
        self.assertTrue(self.manager.submit_text("recovery request"))
        self._wait_for_state(("completed", "idle"))


if __name__ == "__main__":
    unittest.main()
