"""Abstract base class for LLM providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from kivo.conversation.models import ProviderStatus


class LLMProvider(ABC):
    """Abstract interface for LLM response generation engines."""

    @abstractmethod
    async def generate(self, messages: list[dict], **kwargs) -> str:
        """Generate a complete text response."""

    @abstractmethod
    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """Stream response tokens or deltas asynchronously."""

    @abstractmethod
    async def get_status(self) -> ProviderStatus:
        """Return the current provider configuration and availability status."""

    @abstractmethod
    async def close(self) -> None:
        """Release HTTP connections and resources."""

    async def list_models(self) -> list[str]:
        """Return available model IDs supported by this provider."""
        return []

    async def test_connection(self) -> tuple[bool, str]:
        """Test reachability and authentication without exposing secrets."""
        return True, "Provider ready"
