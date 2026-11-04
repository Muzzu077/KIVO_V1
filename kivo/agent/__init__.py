"""KIVO Agent runtime and tool subsystem."""
from __future__ import annotations

from kivo.agent.agent_runtime import AgentRuntime
from kivo.agent.tool_executor import ToolExecutor
from kivo.agent.tool_policy import ToolPolicyError
from kivo.agent.tool_registry import ToolRegistry
from kivo.agent.tool_schema import (
    PermissionLevel,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from kivo.agent.tools.system_info import SYSTEM_OVERVIEW_TOOL
from kivo.agent.tools.time_info import CURRENT_TIME_TOOL


def create_default_tool_registry() -> ToolRegistry:
    """Create a ToolRegistry pre-populated with KIVO's built-in safe read-only tools."""
    registry = ToolRegistry()
    registry.register(CURRENT_TIME_TOOL)
    registry.register(SYSTEM_OVERVIEW_TOOL)
    return registry


__all__ = [
    "AgentRuntime",
    "ToolRegistry",
    "ToolExecutor",
    "ToolDefinition",
    "ToolCall",
    "ToolResult",
    "PermissionLevel",
    "ToolPolicyError",
    "CURRENT_TIME_TOOL",
    "SYSTEM_OVERVIEW_TOOL",
    "create_default_tool_registry",
]
