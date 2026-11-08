"""Orchestrates multi-turn conversation context, LLM responses, streaming, and events."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Callable, Optional

from kivo.agent.agent_runtime import AgentRuntime
from kivo.agent.approval_gate import ApprovalGate
from kivo.agent.authorization import AuthorizationPolicy
from kivo.agent.tool_executor import ToolExecutor
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
from kivo.files.tools import BoundedFileWorker
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
        on_approval_requested: Optional[Callable[[dict], None]] = None,
        on_approval_resolved: Optional[Callable[[dict], None]] = None,
        approval_gate: Optional[ApprovalGate] = None,
        action_registry: Optional[Any] = None,
        authorization_policy: Optional[AuthorizationPolicy] = None,
        turn_timeout_seconds: float = 180.0,
        file_worker: Optional[BoundedFileWorker] = None,
    ):
        self._cfg = load_conversation_config()
        self._context_manager = context_manager or ContextManager(
            max_messages=self._cfg.get("max_messages", 20)
        )

        if provider is not None:
            self._provider = provider
        else:
            self._provider = self._create_provider_from_config(self._cfg)

        self._on_approval_requested = on_approval_requested
        self._on_approval_resolved = on_approval_resolved

        self._approval_gate = approval_gate or ApprovalGate(
            timeout_seconds=60.0,
            on_approval_requested=self._on_approval_req_callback,
            on_approval_resolved=self._on_approval_res_callback,
        )

        self._authorization_policy = authorization_policy or AuthorizationPolicy()
        self._file_worker: Optional[BoundedFileWorker] = file_worker
        self._owns_file_worker = False
        if tool_registry is None:
            # The application normally supplies the worker so it can own final
            # shutdown. A standalone manager creates and therefore owns one.
            if self._file_worker is None:
                self._file_worker = BoundedFileWorker()
                self._owns_file_worker = True
            self._tool_registry = create_default_tool_registry(
                self._approval_gate,
                action_registry=action_registry,
                file_worker=self._file_worker,
            )
        else:
            self._tool_registry = tool_registry
        self._tool_executor = ToolExecutor(
            self._tool_registry,
            approval_gate=self._approval_gate,
            authorization_policy=self._authorization_policy,
        )
        self._agent_runtime = AgentRuntime(
            provider=self._provider,
            registry=self._tool_registry,
            executor=self._tool_executor,
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
        self._turn_timeout_seconds = max(0.01, float(turn_timeout_seconds))

        # One active conversation turn at a time. The ID is issued on submit and
        # is bound to every approval request created by that turn.
        self._active_turn_id: Optional[str] = None
        self._active_turn_started_at = 0.0

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
        self._loop_ready = threading.Event()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name="kivo-conv-loop",
            daemon=True,
        )
        self._loop_thread.start()
        # A submission immediately after construction must see a running loop.
        # The callback is scheduled rather than set before ``run_forever`` so
        # ``is_running()`` is already true when the waiter observes readiness.
        self._loop_ready.wait(timeout=2.0)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        if self._loop:
            self._loop.call_soon(self._loop_ready.set)
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
            cancel_in_flight = not enabled and self._state in ("thinking", "streaming")
        if cancel_in_flight:
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

    def _on_approval_req_callback(self, req_dict: dict) -> None:
        with self._lock:
            self._state = "thinking"
            cmd = req_dict.get("command", "")
            self._last_preview = f"KIVO proposed terminal command: {cmd} (awaiting your approval)"
        self._notify_state()
        if self._on_approval_requested is not None:
            try:
                self._on_approval_requested(req_dict)
            except Exception:
                pass

    def _on_approval_res_callback(self, res_dict: dict) -> None:
        if self._on_approval_resolved is not None:
            try:
                self._on_approval_resolved(res_dict)
            except Exception:
                pass

    def resolve_command_approval(
        self,
        token: str,
        approved: bool,
        turn_id: str = "",
        tool_call_id: str = "",
    ) -> tuple[bool, str]:
        """Resolve a pending approval for exactly the turn that created it.

        Mutation approvals additionally require the exact tool-call identity
        that created the approval request. ``tool_call_id`` stays optional so
        terminal approvals and existing IPC clients remain compatible.
        """
        clean_turn = turn_id.strip() if isinstance(turn_id, str) else ""
        if not clean_turn:
            return False, "A conversation turn_id is required to resolve an approval."
        clean_call = tool_call_id.strip() if isinstance(tool_call_id, str) else ""
        if clean_call:
            return self._approval_gate.resolve(
                token,
                approved,
                turn_id=clean_turn,
                tool_call_id=clean_call,
            )
        return self._approval_gate.resolve(
            token,
            approved,
            turn_id=clean_turn,
        )

    def get_pending_approvals(self) -> list[dict]:
        """Return currently open approval requests."""
        return self._approval_gate.get_pending_requests()

    def get_active_turn_id(self) -> Optional[str]:
        """Return the turn ID currently allowed to own tools and approvals."""
        with self._lock:
            return self._active_turn_id

    def clear(self) -> None:
        """Reset conversation context."""
        self.cancel_active()
        self._approval_gate.cancel_all()
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
        """Submit text into the conversation.

        Only one turn may be active. A second submission is rejected before it
        touches the context, so a slow provider cannot interleave two user
        messages into one assistant turn.
        """
        clean_text = (text or "").strip()
        if not clean_text:
            return False

        # 1. Reserve the single active-turn slot before touching the context.
        with self._lock:
            if self._active_turn_id is not None:
                return False
            turn_id = f"turn_{uuid.uuid4().hex}"
            self._active_turn_id = turn_id
            self._active_turn_started_at = time.time()

        def release_turn() -> None:
            with self._lock:
                if self._active_turn_id == turn_id:
                    self._active_turn_id = None
                    self._active_turn_started_at = 0.0

        # 2. Record user turn in context
        user_msg = self._context_manager.add_user_message(clean_text)
        if self._on_user_message is not None:
            try:
                self._on_user_message(user_msg)
            except Exception:
                pass

        # 3. Check cloud processing consent
        cloud_enabled = bool(self._cfg.get("cloud_enabled", False))
        if not cloud_enabled:
            release_turn()
            with self._lock:
                self._state = "cloud_disabled"
                self._error_message = "Cloud AI is disabled. Enable in Dashboard."
            self._notify_state()
            return False

        # 4. Schedule async response generation
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._generate_response(turn_id),
                    self._loop,
                )
            except Exception:
                release_turn()
                return False
            return True

        release_turn()
        return False

    async def _generate_response(self, turn_id: str) -> None:
        """Coroutine that streams one isolated assistant response."""
        with self._lock:
            if self._active_turn_id != turn_id:
                # Cancelled before the coroutine started.
                return
            self._active_task = asyncio.current_task()
            self._state = "thinking"
            self._error_message = ""
            self._last_preview = ""

        conv_id = self._context_manager.conversation_id
        messages = self._context_manager.get_messages_for_llm()
        self._notify_state()

        if self._on_response_started is not None:
            try:
                self._on_response_started("", conv_id)
            except Exception:
                pass

        def on_delta_callback(chunk: str, accumulated_text: str) -> None:
            with self._lock:
                if self._active_turn_id != turn_id:
                    return
                self._state = "streaming"
                self._last_preview = accumulated_text[:300]
            if self._on_response_delta is not None:
                try:
                    self._on_response_delta(chunk, accumulated_text)
                except Exception:
                    pass

        def on_tool_start_callback(tool_name: str, call_id: str) -> None:
            with self._lock:
                if self._active_turn_id != turn_id:
                    return
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
                if self._active_turn_id != turn_id:
                    return
                status_str = "completed" if result.success else "failed"
                self._last_preview = f"Tool {result.tool_name} {status_str} ({result.execution_time}s)"
            self._notify_state()
            if self._on_tool_completed is not None:
                try:
                    self._on_tool_completed(result)
                except Exception:
                    pass

        try:
            temperature = float(self._cfg.get("temperature", 0.7))
            max_tokens = int(self._cfg.get("max_tokens", 1024))
            model = str(self._cfg.get("model", "auto"))

            full_response = await asyncio.wait_for(
                self._agent_runtime.execute_turn(
                    messages,
                    provider=self._provider,
                    on_delta=on_delta_callback,
                    on_tool_started=on_tool_start_callback,
                    on_tool_completed=on_tool_complete_callback,
                    turn_id=turn_id,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=model,
                ),
                timeout=self._turn_timeout_seconds,
            )

        except asyncio.TimeoutError:
            self._approval_gate.cancel_all(turn_id=turn_id)
            err_msg = (
                f"Conversation turn timed out after "
                f"{self._turn_timeout_seconds:g}s."
            )
            with self._lock:
                was_active = self._active_turn_id == turn_id
                if was_active:
                    self._state = "failed"
                    self._error_message = err_msg
                    self._active_task = None
                    self._active_turn_id = None
                    self._active_turn_started_at = 0.0
            self._notify_state()
            if was_active and self._on_response_failed is not None:
                try:
                    self._on_response_failed(err_msg)
                except Exception:
                    pass
            return

        except asyncio.CancelledError:
            self._approval_gate.cancel_all(turn_id=turn_id)
            with self._lock:
                was_active = self._active_turn_id == turn_id
                if was_active:
                    self._state = "cancelled"
                    self._active_task = None
                    self._active_turn_id = None
                    self._active_turn_started_at = 0.0
            self._notify_state()
            if was_active and self._on_cancelled is not None:
                try:
                    self._on_cancelled()
                except Exception:
                    pass
            return

        except Exception as exc:
            self._approval_gate.cancel_all(turn_id=turn_id)
            err_msg = str(exc)
            with self._lock:
                was_active = self._active_turn_id == turn_id
                if was_active:
                    self._state = "failed"
                    self._error_message = err_msg
                    self._active_task = None
                    self._active_turn_id = None
                    self._active_turn_started_at = 0.0
            self._notify_state()
            if was_active and self._on_response_failed is not None:
                try:
                    self._on_response_failed(err_msg)
                except Exception:
                    pass
            return

        full_response = (full_response or "").strip()

        asst_msg = None
        with self._lock:
            if self._active_turn_id != turn_id:
                # Cancelled while the provider was finishing.
                return
            # Commit the assistant message before releasing the turn so a new
            # submission can never snapshot the context without this reply.
            if full_response:
                asst_msg = self._context_manager.add_assistant_message(full_response)
            self._active_task = None
            self._active_turn_id = None
            self._active_turn_started_at = 0.0
            self._state = "completed"
            if full_response:
                self._last_preview = full_response[:300]

        if asst_msg is not None and self._on_response_completed is not None:
            try:
                self._on_response_completed(asst_msg)
            except Exception:
                pass

        self._notify_state()

        # Briefly reset to idle
        await asyncio.sleep(0.1)
        with self._lock:
            if self._state == "completed" and self._active_turn_id is None:
                self._state = "idle"
        self._notify_state()

    def cancel_active(self) -> bool:
        """Cancel the current turn and every approval bound to it."""
        with self._lock:
            turn_id = self._active_turn_id
            task = self._active_task
            if turn_id is None and (task is None or task.done()):
                return False
            self._state = "cancelled"
            self._active_turn_id = None
            self._active_turn_started_at = 0.0
            self._active_task = None
            loop = self._loop

        if turn_id is not None:
            self._approval_gate.cancel_all(turn_id=turn_id)
        if task is not None and not task.done():
            try:
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(task.cancel)
                else:
                    task.cancel()
            except RuntimeError:
                pass

        # The generation coroutine suppresses its own callback when the turn
        # was already cleared, so emit the cancellation event here exactly once.
        if self._on_cancelled is not None:
            try:
                self._on_cancelled()
            except Exception:
                pass
        self._notify_state()
        return True

    def _notify_state(self) -> None:
        if self._on_state_change is not None:
            try:
                self._on_state_change(self.get_status())
            except Exception:
                pass

    def close(self) -> None:
        """Stop background event loop and release provider connections.

        The application owns final file-worker shutdown. Conversation teardown
        first cancels active filesystem work and every pending approval; a
        standalone manager that created its own worker also closes it here.
        """
        self.cancel_active()
        self._approval_gate.cancel_all()
        if self._file_worker is not None:
            try:
                self._file_worker.cancel_active()
            except Exception:
                pass
            if self._owns_file_worker:
                try:
                    self._file_worker.close()
                except Exception:
                    pass
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
