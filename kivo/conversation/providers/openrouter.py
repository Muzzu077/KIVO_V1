"""OpenRouter LLM provider implementation using httpx async client."""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

from kivo.conversation.exceptions import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from kivo.conversation.models import ProviderStatus
from kivo.conversation.providers.base import LLMProvider

OPENROUTER_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 30.0


class OpenRouterProvider(LLMProvider):
    """Asynchronous OpenRouter provider supporting streaming and non-streaming responses."""

    def __init__(
        self,
        api_key_resolver,
        model: str = "meta-llama/llama-3.3-70b-instruct:free",
        cloud_enabled_getter=lambda: False,
        base_url: str = OPENROUTER_COMPLETIONS_URL,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ):
        self._key_resolver = api_key_resolver
        self._model = model
        self._cloud_enabled_getter = cloud_enabled_getter
        self._base_url = base_url
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            timeout = httpx.Timeout(
                connect=self._connect_timeout,
                read=self._read_timeout,
                write=self._connect_timeout,
                pool=self._connect_timeout,
            )
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    async def get_status(self) -> ProviderStatus:
        cloud_enabled = bool(self._cloud_enabled_getter())
        api_key = self._key_resolver()
        has_key = bool(api_key and len(api_key) > 5)

        available = cloud_enabled and has_key
        error_msg = ""
        if not cloud_enabled:
            error_msg = "Cloud AI is disabled. Enable in Dashboard."
        elif not has_key:
            error_msg = "OpenRouter API key is not configured."

        return ProviderStatus(
            provider_name="openrouter",
            model=self._model,
            cloud_enabled=cloud_enabled,
            available=available,
            error_message=error_msg,
        )

    def _build_headers(self) -> dict[str, str]:
        key = self._key_resolver()
        if not key:
            raise ProviderAuthenticationError("OpenRouter API key is missing. Set OPENROUTER_API_KEY.")
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Muzzu077/KIVO_V1",
            "X-Title": "KIVO Desktop Assistant",
        }

    async def generate(self, messages: list[dict], **kwargs) -> str:
        """Non-streaming text completion."""
        chunks = []
        async for chunk in self.stream(messages, **kwargs):
            chunks.append(chunk)
        return "".join(chunks).strip()

    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """Stream response chunks from OpenRouter using Server-Sent Events."""
        client = self._get_client()
        headers = self._build_headers()

        payload = {
            "model": kwargs.get("model", self._model),
            "messages": messages,
            "stream": True,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 1024),
        }

        try:
            async with client.stream(
                "POST",
                self._base_url,
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code == 401:
                    raise ProviderAuthenticationError("OpenRouter rejected the API key (401 Unauthorized).")
                if response.status_code == 402:
                    raise ProviderError("OpenRouter account has insufficient credits (402 Payment Required).")
                if response.status_code == 429:
                    raise ProviderRateLimitError("OpenRouter rate limit reached. Please wait a moment (429).")
                if response.status_code >= 400:
                    err_body = (await response.aread()).decode("utf-8", errors="replace")
                    try:
                        err_json = json.loads(err_body)
                        msg = err_json.get("error", {}).get("message", err_body[:200])
                    except Exception:
                        msg = err_body[:200]
                    raise ProviderError(f"OpenRouter error ({response.status_code}): {msg}")

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    clean_line = line.strip()
                    # Skip SSE comments and metadata
                    if clean_line.startswith(":"):
                        continue
                    if clean_line.startswith("data:"):
                        data_str = clean_line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            choices = data.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content")
                                if content:
                                    yield content
                        except json.JSONDecodeError:
                            continue

        except httpx.ConnectTimeout as exc:
            raise ProviderTimeoutError("Connection to OpenRouter timed out.") from exc
        except httpx.ReadTimeout as exc:
            raise ProviderTimeoutError("OpenRouter response streaming timed out.") from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"Network error connecting to OpenRouter: {exc}") from exc

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
