"""Custom exceptions for the KIVO conversation subsystem."""
from __future__ import annotations


class ConversationError(Exception):
    """Base exception for conversation errors."""


class CloudConsentRequiredError(ConversationError):
    """Raised when an LLM request is attempted while cloud processing is disabled."""


class ProviderError(ConversationError):
    """Raised when an LLM provider request fails."""


class ProviderAuthenticationError(ProviderError):
    """Raised when provider API key is missing or rejected."""


class ProviderRateLimitError(ProviderError):
    """Raised when provider rate limits are exceeded."""


class ProviderTimeoutError(ProviderError):
    """Raised when provider request exceeds timeout limit."""


class ContextBudgetExceededError(ConversationError):
    """Raised when conversation context exceeds memory bounds."""
