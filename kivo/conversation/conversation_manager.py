"""Orchestrates multi-turn conversation context, LLM responses, streaming, and events."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional

from kivo.agent.agent_runtime import AgentRuntime
from kivo.agent.tool_registry import ToolRegistry
from kivo.agent.tool_schema import ToolResult
from kivo.agent import create_default_tool_registry
from kivo.conversation.context_manager import ContextManager
from kivo.conversation.conversation_config import (
    DEFAULT_OMNIROUTE_URL,
    clean_base_url,
    get_omniroute_api_key,
    get_openrouter_api_key,
    load_conversation_config,
    save_conversation_config,
)
from kivo.conversation.models import (
    ConversationStatus,
    Message,
    ProviderStatus,
)
from kivo.conversation.providers.base import LLMProvider
from kivo.conversation.providers.openrouter import (
    OmniRouteProvider,
    OpenRouterProvider,
)
from kivo.stt.models import TranscriptionResult


class ConversationManager:
    """Manages conversational dialogue turns, LLM generation, and streaming events."""

    def __init__(
        self,
        context_manager: Optional[ContextManager] = None,
        provider: Optional[LLMProvider] = None,
        tool_registry: Optional[ToolRegistry] = None,
        on_state_change: Optional[Callable[[ConversationStatus], None]] = None,
        on_user_message: Optional[Callable[[Message], None]] = None,
        on_response_started: Optional[Callable[[str, str], None]] = None,
        on_response_delta: Optional[Callable[[str, str], None]] = None,
        on_response_completed: Optional[Callable[[Message], None]] = None,
        on_response_failed: Optional[Callable[[str], None]] = None,
        on_cancelled: Optional[Callable[[], None]] = None,
        on_tool_started: Optional[Callable[[str, str], None]] = None,
        on_tool_completed: Optional[Callable[[ToolResult], None]] = None,
    ):
        self._cfg = load_conversation_config()
        self._context_manager = context_manager or ContextManager(
            max_messages=self._cfg.get("max_messages", 20)
        )

        if provider is not None:
            self._provider = provider
        else:
            self._provider = self._create_provider_from_config(self._cfg)

        self._tool_registry = tool_registry or create_default_tool_registry()
        self._agent_runtime = AgentRuntime(
            provider=self._provider,
            registry=self._tool_registry,
        )

        self._on_state_change = on_state_change
        self._on_user_message = on_user_message
        self._on_response_started = on_response_started
        self._on_response_delta = on_response_delta
        self._on_response_completed = on_response_completed
        self._on_response_failed = on_response_failed
        self._on_cancelled = on_cancelled
        self._on_tool_started = on_tool_started
        self._on_tool_completed = on_tool_completed

        self._lock = threading.Lock()
        self._state = "idle"
        self._error_message = ""
        self._last_preview = ""
        self._processed_captures: set[str] = set()

        # Active in-flight generation task
        self._active_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._init_event_loop()

    def _create_provider_from_config(self, cfg: dict) -> LLMProvider:
        p_name = cfg.get("provider", "omniroute").strip().lower()
        cloud_getter = lambda: self._cfg.get("cloud_enabled", False)

        if p_name == "openrouter":
            return OpenRouterProvider(
                api_key_resolver=get_openrouter_api_key,
                model=cfg.get("model", "meta-llama/llama-3.3-70b-instruct:free"),
                cloud_enabled_getter=cloud_getter,
            )
        # Default: omniroute
        base_url = clean_base_url(cfg.get("omniroute_base_url", DEFAULT_OMNIROUTE_URL))
        return OmniRouteProvider(
            api_key_resolver=get_omniroute_api_key,
            model=cfg.get("model", "agy/gemini-3.8-flash-high"),
            cloud_enabled_getter=cloud_getter,
            base_url=base_url,
        )

    def _init_event_loop(self) -> None:
        """Dedicated background asyncio event loop for LLM requests."""
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name="kivo-conv-loop",
            daemon=True,
        )
        self._loop_thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        if self._loop:
            self._loop.run_forever()

    def get_status(self) -> ConversationStatus:
        """Return the current conversation status snapshot."""
        with self._lock:
            state = self._state
            err = self._error_message
            preview = self._last_preview

        cloud_enabled = bool(self._cfg.get("cloud_enabled", False))
        msg_count = self._context_manager.message_count()
        conv_id = self._context_manager.conversation_id
        provider_name = str(self._cfg.get("provider", "omniroute")).strip().lower()

        has_key = bool(get_omniroute_api_key()) if provider_name == "omniroute" else bool(get_openrouter_api_key())
        available = cloud_enabled and has_key

        prov_status = ProviderStatus(
            provider_name=provider_name,
            model=str(self._cfg.get("model", "")),
            cloud_enabled=cloud_enabled,
            available=available,
        )

        return ConversationStatus(
            state=state,
            active_conversation_id=conv_id,
            message_count=msg_count,
            cloud_enabled=cloud_enabled,
            provider=prov_status,
            last_response_preview=preview,
            error_message=err,
        )

    def get_history(self) -> list[dict]:
        """Return message history for the active conversation."""
        return [m.to_dict() for m in self._context_manager.get_history()]

    def set_cloud_enabled(self, enabled: bool) -> None:
        """Update user consent for cloud AI processing."""
        with self._lock:
            self._cfg["cloud_enabled"] = bool(enabled)
            save_conversation_config(cloud_enabled=bool(enabled))
            if not enabled and self._state in ("thinking", "streaming"):
                self.cancel_active()
        self._notify_state()

    def set_model(self, model: str) -> None:
        """Update selected model ID."""
        clean_model = (model or "").strip()
        if not clean_model:
            return
        with self._lock:
            self._cfg["model"] = clean_model
            save_conversation_config(model=clean_model)
            if hasattr(self._provider, "_model"):
                self._provider._model = clean_model
        self._notify_state()

    def set_provider(
        self,
        provider_name: str,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Switch active provider ('omniroute' or 'openrouter') and rebuild provider instance."""
        p_name = (provider_name or "").strip().lower()
        if p_name not in ("omniroute", "openrouter"):
            raise ValueError(f"Unsupported provider: {provider_name}")

        self.cancel_active()
        with self._lock:
            self._cfg["provider"] = p_name
            if base_url:
                self._cfg["omniroute_base_url"] = clean_base_url(base_url)
            if model:
                self._cfg["model"] = model.strip()
            save_conversation_config(
                provider=p_name,
                omniroute_base_url=self._cfg.get("omniroute_base_url"),
                model=self._cfg.get("model"),
            )
            old_prov = self._provider
            self._provider = self._create_provider_from_config(self._cfg)
            if hasattr(self, "_agent_runtime"):
                self._agent_runtime._provider = self._provider

        if hasattr(old_prov, "close"):
            try:
                # Schedule closing old HTTP client
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(old_prov.close(), self._loop)
            except Exception:
                pass
        self._notify_state()

    def set_omniroute_base_url(self, base_url: str) -> None:
        """Update OmniRoute gateway endpoint URL."""
        cleaned = clean_base_url(base_url)
        with self._lock:
            self._cfg["omniroute_base_url"] = cleaned
            save_conversation_config(omniroute_base_url=cleaned)
            if hasattr(self._provider, "_base_url"):
                self._provider._base_url = cleaned
        self._notify_state()

    def list_models(self) -> list[str]:
        """Fetch available models from the active provider synchronously."""
        if not self._loop or not self._loop.is_running():
            return []
        future = asyncio.run_coroutine_threadsafe(self._provider.list_models(), self._loop)
        return future.result(timeout=12.0)

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to the active provider synchronously."""
        if not self._loop or not self._loop.is_running():
            return False, "Background conversation event loop is not running"
        future = asyncio.run_coroutine_threadsafe(self._provider.test_connection(), self._loop)
        return future.result(timeout=12.0)

    def clear(self) -> None:
        """Reset conversation context."""
        self.cancel_active()
        self._context_manager.clear()
        with self._lock:
            self._state = "idle"
            self._error_message = ""
            self._last_preview = ""
            self._processed_captures.clear()
        self._notify_state()

    def submit_transcript(self, result: TranscriptionResult) -> bool:
        """Receive a completed speech transcription from Stage 7."""
        if not isinstance(result, TranscriptionResult):
            return False

        # Deduplicate: ensure a capture is processed only once
        if result.capture_id:
            with self._lock:
                if result.capture_id in self._processed_captures:
                    return False
                self._processed_captures.add(result.capture_id)

        clean_text = (result.text or "").strip()
        if not clean_text or result.status != "completed":
            return False

        return self.submit_text(clean_text)

    def submit_text(self, text: str) -> bool:
        """Submit text into the conversation."""
        clean_text = (text or "").strip()
        if not clean_text:
            return False

        # 1. Record user turn in context
        user_msg = self._context_manager.add_user_message(clean_text)
        if self._on_user_message is not None:
            try:
                self._on_user_message(user_msg)
            except Exception:
                pass

        # 2. Check cloud processing consent
        cloud_enabled = bool(self._cfg.get("cloud_enabled", False))
        if not cloud_enabled:
            with self._lock:
                self._state = "cloud_disabled"
                self._error_message = "Cloud AI is disabled. Enable in Dashboard."
            self._notify_state()
            return False

        # 3. Schedule async response generation
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._generate_response(), self._loop)
            return True
        return False

    async def _generate_response(self) -> None:
        """Coroutine that streams the assistant response from the provider."""
        conv_id = self._context_manager.conversation_id
        messages = self._context_manager.get_messages_for_llm()

        with self._lock:
            self._state = "thinking"
            self._error_message = ""
            self._last_preview = ""
        self._notify_state()

        if self._on_response_started is not None:
            try:
                self._on_response_started("", conv_id)
            except Exception:
                pass

        def on_delta_callback(chunk: str, accumulated_text: str) -> None:
            with self._lock:
                self._state = "streaming"
                self._last_preview = accumulated_text[:300]
            if self._on_response_delta is not None:
                try:
                    self._on_response_delta(chunk, accumulated_text)
                except Exception:
                    pass

        def on_tool_start_callback(tool_name: str, call_id: str) -> None:
            with self._lock:
                self._state = "thinking"
                self._last_preview = f"KIVO is checking {tool_name}..."
            self._notify_state()
            if self._on_tool_started is not None:
                try:
                    self._on_tool_started(tool_name, call_id)
                except Exception:
                    pass

        def on_tool_complete_callback(result: ToolResult) -> None:
            with self._lock:
                status_str = "completed" if result.success else "failed"
                self._last_preview = f"Tool {result.tool_name} {status_str} ({result.execution_time}s)"
            self._notify_state()
            if self._on_tool_completed is not None:
                try:
                    self._on_tool_completed(result)
                except Exception:
                    pass

        try:
            current_task = asyncio.current_task()
            with self._lock:
                self._active_task = current_task

            temperature = float(self._cfg.get("temperature", 0.7))
            max_tokens = int(self._cfg.get("max_tokens", 1024))
            model = str(self._cfg.get("model", "auto"))

            full_response = await self._agent_runtime.execute_turn(
                messages,
                provider=self._provider,
                on_delta=on_delta_callback,
                on_tool_started=on_tool_start_callback,
                on_tool_completed=on_tool_complete_callback,
                temperature=temperature,
                max_tokens=max_tokens,
                model=model,
            )

        except asyncio.CancelledError:
            with self._lock:
                self._state = "cancelled"
                self._active_task = None
            if self._on_cancelled is not None:
                try:
                    self._on_cancelled()
                except Exception:
                    pass
            self._notify_state()
            return

        except Exception as exc:
            err_msg = str(exc)
            with self._lock:
                self._state = "failed"
                self._error_message = err_msg
                self._active_task = None
            if self._on_response_failed is not None:
                try:
                    self._on_response_failed(err_msg)
                except Exception:
                    pass
            self._notify_state()
            return

        with self._lock:
            self._active_task = None

        full_response = (full_response or "").strip()
        if full_response:
            asst_msg = self._context_manager.add_assistant_message(full_response)
            with self._lock:
                self._state = "completed"
                self._last_preview = full_response[:300]

            if self._on_response_completed is not None:
                try:
                    self._on_response_completed(asst_msg)
                except Exception:
                    pass
        else:
            with self._lock:
                self._state = "completed"

        self._notify_state()

        # Briefly reset to idle
        time.sleep(0.1)
        with self._lock:
            if self._state == "completed":
                self._state = "idle"
        self._notify_state()

    def cancel_active(self) -> bool:
        """Cancel the current in-flight LLM generation task."""
        with self._lock:
            task = self._active_task
            self._active_task = None
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    def _notify_state(self) -> None:
        if self._on_state_change is not None:
            try:
                self._on_state_change(self.get_status())
            except Exception:
                pass

    def close(self) -> None:
        """Stop background event loop and release provider connections."""
        self.cancel_active()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=2.0)
        if hasattr(self._provider, "close"):
            try:
                # Synchronous close
                if hasattr(self._provider, "_client") and self._provider._client:
                    # Async close in temporary loop if needed
                    pass
            except Exception:
                pass
