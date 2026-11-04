"""Data models for KIVO conversational brain and context."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Message:
    """A single turn in a conversation."""

    role: str  # "system" | "user" | "assistant"
    content: str
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        # Bounded content length defensively (max 12,000 chars per message)
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content[:12000] if self.content else "",
            "timestamp": round(self.timestamp, 3),
        }

    def to_llm_dict(self) -> dict:
        """Formatted dictionary for OpenAI/OpenRouter chat completions API."""
        return {
            "role": self.role,
            "content": self.content,
        }


@dataclass
class Conversation:
    """An active conversation session."""

    conversation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[Message] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "messages": [m.to_dict() for m in self.messages],
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
        }


@dataclass
class ProviderStatus:
    """Status snapshot of the LLM provider."""

    provider_name: str = "openrouter"
    model: str = "meta-llama/llama-3.3-70b-instruct:free"
    cloud_enabled: bool = False
    available: bool = False
    error_message: str = ""

    def to_dict(self) -> dict:
        return {
            "provider_name": self.provider_name,
            "model": self.model,
            "cloud_enabled": self.cloud_enabled,
            "available": self.available,
            "error_message": self.error_message,
        }


@dataclass
class ConversationStatus:
    """Live status snapshot of the conversational subsystem."""

    state: str = "idle"  # "idle" | "preparing" | "thinking" | "streaming" | "completed" | "cancelled" | "failed" | "cloud_disabled"
    active_conversation_id: str = ""
    message_count: int = 0
    cloud_enabled: bool = False
    provider: ProviderStatus = field(default_factory=ProviderStatus)
    last_response_preview: str = ""
    error_message: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "active_conversation_id": self.active_conversation_id,
            "message_count": self.message_count,
            "cloud_enabled": self.cloud_enabled,
            "provider": self.provider.to_dict(),
            "last_response_preview": self.last_response_preview[:300],
            "error_message": self.error_message,
        }
