"""In-memory bounded context manager for multi-turn conversations."""
from __future__ import annotations

import collections
import threading
import time
import uuid
from typing import Optional

from kivo.conversation.models import Conversation, Message
from kivo.conversation.prompt_manager import get_system_prompt

MAX_TOTAL_CONTEXT_CHARS = 16000  # Defensive boundary to protect context window


class ContextManager:
    """Manages active conversation context and memory bounds."""

    def __init__(self, max_messages: int = 20):
        self._max_messages = max(2, max_messages)
        self._lock = threading.Lock()
        self._conversation_id = str(uuid.uuid4())
        self._messages: collections.deque[Message] = collections.deque(maxlen=self._max_messages)
        self._created_at = time.time()
        self._updated_at = time.time()

    @property
    def conversation_id(self) -> str:
        with self._lock:
            return self._conversation_id

    def add_user_message(self, text: str) -> Message:
        """Record a user speech transcript in the conversation."""
        clean_text = (text or "").strip()
        msg = Message(role="user", content=clean_text)
        with self._lock:
            self._messages.append(msg)
            self._updated_at = time.time()
        return msg

    def add_assistant_message(self, text: str) -> Message:
        """Record an assistant response in the conversation."""
        clean_text = (text or "").strip()
        msg = Message(role="assistant", content=clean_text)
        with self._lock:
            self._messages.append(msg)
            self._updated_at = time.time()
        return msg

    def get_messages_for_llm(self) -> list[dict]:
        """Construct the message payload for the LLM API call.

        Includes system prompt followed by bounded conversation history.
        Truncates oldest messages if character count exceeds MAX_TOTAL_CONTEXT_CHARS.
        """
        with self._lock:
            active_msgs = list(self._messages)

        system_prompt = get_system_prompt()
        llm_messages = [{"role": "system", "content": system_prompt}]

        # Budget enforcement: count from newest to oldest
        selected: list[dict] = []
        char_count = len(system_prompt)

        for msg in reversed(active_msgs):
            msg_dict = msg.to_llm_dict()
            msg_len = len(msg_dict["content"])
            if char_count + msg_len > MAX_TOTAL_CONTEXT_CHARS and selected:
                break
            selected.append(msg_dict)
            char_count += msg_len

        selected.reverse()
        llm_messages.extend(selected)
        return llm_messages

    def get_history(self) -> list[Message]:
        with self._lock:
            return list(self._messages)

    def get_conversation_snapshot(self) -> Conversation:
        with self._lock:
            return Conversation(
                conversation_id=self._conversation_id,
                messages=list(self._messages),
                created_at=self._created_at,
                updated_at=self._updated_at,
            )

    def message_count(self) -> int:
        with self._lock:
            return len(self._messages)

    def clear(self) -> None:
        """Reset conversation context and start a fresh session."""
        with self._lock:
            self._messages.clear()
            self._conversation_id = str(uuid.uuid4())
            self._created_at = time.time()
            self._updated_at = time.time()
