"""OpenAI-compatible LLM provider implementations for OpenRouter and OmniRoute."""
from __future__ import annotations

import json
from typing import AsyncIterator, Callable, Optional

import httpx

from kivo.conversation.exceptions import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from kivo.conversation.models import ProviderStatus
from kivo.conversation.providers.base import LLMProvider

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 30.0

OPENROUTER_DEFAULT_URL = "https://openrouter.ai/api/v1"
OMNIROUTE_DEFAULT_URL = "http://127.0.0.1:20128/v1"


class OpenAICompatibleProvider(LLMProvider):
    """General OpenAI-compatible chat completions provider (OmniRoute, OpenRouter, etc.)."""

    def __init__(
        self,
        provider_name: str,
        api_key_resolver: Callable[[], str],
        model: str,
        cloud_enabled_getter: Callable[[], bool] = lambda: False,
        base_url: str = OPENROUTER_DEFAULT_URL,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ):
        self._provider_name = provider_name.strip().lower()
        self._key_resolver = api_key_resolver
        self._model = model.strip()
        self._cloud_enabled_getter = cloud_enabled_getter
        self._base_url = base_url.strip().rstrip("/")
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _get_chat_url(self) -> str:
        url = self._base_url
        if url.endswith("/chat/completions"):
            return url
        return f"{url}/chat/completions"

    def _get_models_url(self) -> str:
        url = self._base_url
        if url.endswith("/chat/completions"):
            url = url[:-len("/chat/completions")]
        return f"{url}/models"

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
        has_key = bool(api_key and len(api_key) > 3)

        available = cloud_enabled and has_key
        error_msg = ""
        if not cloud_enabled:
            error_msg = "Cloud AI is disabled. Enable in Dashboard."
        elif not has_key:
            error_msg = f"{self._provider_name.title()} API key is not configured."

        return ProviderStatus(
            provider_name=self._provider_name,
            model=self._model,
            cloud_enabled=cloud_enabled,
            available=available,
            error_message=error_msg,
        )

    def _build_headers(self) -> dict[str, str]:
        key = self._key_resolver()
        if not key:
            raise ProviderAuthenticationError(
                f"{self._provider_name.title()} API key is missing. Please configure it in settings."
            )
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Muzzu077/KIVO_V1",
            "X-Title": "KIVO Desktop Assistant",
        }
        return headers

    async def list_models(self) -> list[str]:
        """Fetch available model IDs from the endpoint (GET /v1/models)."""
        client = self._get_client()
        headers = self._build_headers()
        models_url = self._get_models_url()

        try:
            response = await client.get(models_url, headers=headers)
            if response.status_code == 401:
                raise ProviderAuthenticationError(
                    f"{self._provider_name.title()} rejected the API key (401 Unauthorized)."
                )
            if response.status_code >= 400:
                raise ProviderError(
                    f"{self._provider_name.title()} returned error {response.status_code} while listing models."
                )

            data = response.json()
            model_items = data.get("data", [])
            models = []
            if isinstance(model_items, list):
                for item in model_items:
                    if isinstance(item, dict) and "id" in item:
                        mid = str(item["id"]).strip()
                        if mid:
                            models.append(mid)
            return sorted(models)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderError(
                f"Cannot connect to {self._provider_name.title()} at {models_url}. Is the service running?"
            ) from exc
        except httpx.ReadTimeout as exc:
            raise ProviderTimeoutError(
                f"Request to {self._provider_name.title()} timed out while discovering models."
            ) from exc
        except Exception as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"Failed to list models from {self._provider_name.title()}: {exc}") from exc

    async def test_connection(self) -> tuple[bool, str]:
        """Validate endpoint reachability and credentials without leaking the secret."""
        try:
            models = await self.list_models()
            return True, f"Successfully connected to {self._provider_name.title()} ({len(models)} models available)."
        except Exception as exc:
            return False, str(exc)

    async def generate(self, messages: list[dict], **kwargs) -> str:
        """Non-streaming text completion."""
        chunks = []
        async for chunk in self.stream(messages, **kwargs):
            chunks.append(chunk)
        return "".join(chunks).strip()

    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """Stream response chunks from the OpenAI-compatible endpoint using SSE."""
        client = self._get_client()
        headers = self._build_headers()
        chat_url = self._get_chat_url()

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
                chat_url,
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code == 401:
                    raise ProviderAuthenticationError(
                        f"{self._provider_name.title()} rejected the API key (401 Unauthorized)."
                    )
                if response.status_code == 402:
                    raise ProviderError(
                        f"{self._provider_name.title()} account has insufficient credits (402 Payment Required)."
                    )
                if response.status_code == 429:
                    raise ProviderRateLimitError(
                        f"{self._provider_name.title()} rate limit reached. Please wait a moment (429)."
                    )
                if response.status_code >= 400:
                    err_body = (await response.aread()).decode("utf-8", errors="replace")
                    try:
                        err_json = json.loads(err_body)
                        msg = err_json.get("error", {}).get("message", err_body[:200])
                    except Exception:
                        msg = err_body[:200]
                    raise ProviderError(f"{self._provider_name.title()} error ({response.status_code}): {msg}")

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    clean_line = line.strip()
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
                                # Plain text responses only — ignore tool calls
                                content = delta.get("content")
                                if content:
                                    yield content
                        except json.JSONDecodeError:
                            continue

        except httpx.ConnectTimeout as exc:
            raise ProviderTimeoutError(f"Connection to {self._provider_name.title()} timed out.") from exc
        except httpx.ReadTimeout as exc:
            raise ProviderTimeoutError(f"{self._provider_name.title()} response streaming timed out.") from exc
        except httpx.RequestError as exc:
            raise ProviderError(f"Network error connecting to {self._provider_name.title()}: {exc}") from exc

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter provider preset."""

    def __init__(
        self,
        api_key_resolver: Callable[[], str],
        model: str = "meta-llama/llama-3.3-70b-instruct:free",
        cloud_enabled_getter: Callable[[], bool] = lambda: False,
        base_url: str = OPENROUTER_DEFAULT_URL,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ):
        super().__init__(
            provider_name="openrouter",
            api_key_resolver=api_key_resolver,
            model=model,
            cloud_enabled_getter=cloud_enabled_getter,
            base_url=base_url,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )


class OmniRouteProvider(OpenAICompatibleProvider):
    """Local OmniRoute gateway provider preset."""

    def __init__(
        self,
        api_key_resolver: Callable[[], str],
        model: str = "agy/gemini-3.8-flash-high",
        cloud_enabled_getter: Callable[[], bool] = lambda: False,
        base_url: str = OMNIROUTE_DEFAULT_URL,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ):
        super().__init__(
            provider_name="omniroute",
            api_key_resolver=api_key_resolver,
            model=model,
            cloud_enabled_getter=cloud_enabled_getter,
            base_url=base_url,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
