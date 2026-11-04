"""Agent runtime loop coordinating LLM tool calls and safe tool execution."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, Optional, TYPE_CHECKING

from kivo.agent.tool_executor import ToolExecutor
from kivo.agent.tool_policy import MAX_AGENT_ROUNDS
from kivo.agent.tool_registry import ToolRegistry
from kivo.agent.tool_schema import ToolCall, ToolResult

if TYPE_CHECKING:
    from kivo.conversation.providers.base import LLMProvider


class AgentRuntime:
    """Manages the multi-round execution cycle between LLM provider and registered tools."""

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        executor: Optional[ToolExecutor] = None,
        max_rounds: int = MAX_AGENT_ROUNDS,
    ):
        self._provider = provider
        self._registry = registry
        self._executor = executor or ToolExecutor(registry)
        self._max_rounds = max(1, min(10, max_rounds))

    async def execute_turn(
        self,
        messages: list[dict],
        provider: Optional[LLMProvider] = None,
        on_delta: Optional[Callable[[str, str], None]] = None,
        on_tool_started: Optional[Callable[[str, str], None]] = None,
        on_tool_completed: Optional[Callable[[ToolResult], None]] = None,
        **kwargs,
    ) -> str:
        """Run the bounded agent turn, executing tools and continuing until model answers."""
        active_provider = provider or self._provider
        executed_ids: set[str] = set()
        accumulated_final: list[str] = []

        tools_schema = self._registry.get_openai_tools()

        for round_idx in range(self._max_rounds):
            round_text_chunks: list[str] = []
            tool_calls_requested: list[ToolCall] = []

            # Only provide tool schemas if there are registered tools
            avail_tools = tools_schema if tools_schema else None

            async for chunk in active_provider.stream(messages, tools=avail_tools, **kwargs):
                if isinstance(chunk, dict) and chunk.get("type") == "tool_calls":
                    tcs = chunk.get("tool_calls", [])
                    tool_calls_requested.extend(tcs)
                elif isinstance(chunk, str):
                    round_text_chunks.append(chunk)
                    if on_delta is not None:
                        current_text = "".join(accumulated_final) + "".join(round_text_chunks)
                        try:
                            on_delta(chunk, current_text)
                        except Exception:
                            pass

            round_text = "".join(round_text_chunks).strip()
            if round_text:
                accumulated_final.append(round_text)

            # If the model produced tool calls, execute them and continue the loop
            if tool_calls_requested:
                # 1. Append assistant turn with tool_calls
                asst_msg = {
                    "role": "assistant",
                    "content": round_text if round_text else None,
                    "tool_calls": [tc.to_openai_dict() for tc in tool_calls_requested],
                }
                messages.append(asst_msg)

                # 2. Execute each tool and append role='tool' message
                for call in tool_calls_requested:
                    if on_tool_started is not None:
                        try:
                            on_tool_started(call.tool_name, call.tool_call_id)
                        except Exception:
                            pass

                    result = await self._executor.execute(call, executed_ids)

                    if on_tool_completed is not None:
                        try:
                            on_tool_completed(result)
                        except Exception:
                            pass

                    messages.append(result.to_tool_message())

                # Continue to next round so the model can process the tool results
                continue

            # Model finished with text and requested no more tools
            break

        return "".join(accumulated_final).strip()
