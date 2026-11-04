"""Safe execution runner for agent tools with timeouts and error containment."""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Optional

from kivo.agent.tool_policy import (
    MAX_EXECUTION_TIMEOUT,
    ToolPolicyError,
    validate_tool_call,
)
from kivo.agent.tool_registry import ToolRegistry
from kivo.agent.tool_schema import ToolCall, ToolResult


class ToolExecutor:
    """Executes validated tool calls safely within timeout boundaries."""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    async def execute(
        self,
        call: ToolCall,
        executed_ids: set[str],
    ) -> ToolResult:
        """Validate and execute a tool call asynchronously."""
        tool_name = call.tool_name
        t0 = time.monotonic()

        # Check tool registration
        tool_def = self._registry.get_tool(tool_name)
        if tool_def is None:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=tool_name,
                output={"error": f"Tool '{tool_name}' is not registered or supported in this runtime."},
                success=False,
                execution_time=round(time.monotonic() - t0, 3),
                error_message=f"Unknown tool: '{tool_name}'",
            )

        # Policy validation
        try:
            validate_tool_call(call, tool_def, executed_ids)
        except ToolPolicyError as exc:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=tool_name,
                output={"error": f"Security policy blocked tool '{tool_name}': {exc}"},
                success=False,
                execution_time=round(time.monotonic() - t0, 3),
                error_message=str(exc),
            )

        # Record tool_call_id to prevent replay
        executed_ids.add(call.tool_call_id)

        timeout = min(tool_def.timeout_seconds, MAX_EXECUTION_TIMEOUT)
        try:
            sig = inspect.signature(tool_def.handler)
            extra_kwargs: dict[str, Any] = {}
            if "tool_call_id" in sig.parameters:
                extra_kwargs["tool_call_id"] = call.tool_call_id

            if inspect.iscoroutinefunction(tool_def.handler):
                output = await asyncio.wait_for(
                    tool_def.handler(call.arguments, **extra_kwargs),
                    timeout=timeout,
                )
            else:
                # Run synchronous handler in default loop executor
                loop = asyncio.get_running_loop()
                output = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: tool_def.handler(call.arguments, **extra_kwargs)),
                    timeout=timeout,
                )
            execution_time = time.monotonic() - t0
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=tool_name,
                output=output,
                success=True,
                execution_time=round(execution_time, 3),
            )

        except asyncio.TimeoutError:
            execution_time = time.monotonic() - t0
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=tool_name,
                output={"error": f"Tool '{tool_name}' execution timed out after {timeout}s."},
                success=False,
                execution_time=round(execution_time, 3),
                error_message="Execution timed out",
            )
        except Exception as exc:
            execution_time = time.monotonic() - t0
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=tool_name,
                output={"error": f"Tool '{tool_name}' raised an error: {exc}"},
                success=False,
                execution_time=round(execution_time, 3),
                error_message=str(exc),
            )
