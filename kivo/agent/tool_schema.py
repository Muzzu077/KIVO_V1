"""Tool schemas, models, and permission classifications for the KIVO agent runtime."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class PermissionLevel(str, Enum):
    """Execution permission classification for agent tools."""

    READ_ONLY = "read_only"                     # Safe inspection (time, system telemetry)
    CONFIRMATION_REQUIRED = "confirmation"      # State modification requiring explicit approval
    BLOCKED = "blocked"                         # Dangerous / forbidden actions (eval, shell delete)


@dataclass(frozen=True)
class ToolCall:
    """A tool call requested by the LLM."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_openai_dict(self) -> dict:
        return {
            "id": self.tool_call_id,
            "type": "function",
            "function": {
                "name": self.tool_name,
                "arguments": json.dumps(self.arguments),
            },
        }


@dataclass(frozen=True)
class ToolResult:
    """Outcome of a tool execution."""

    tool_call_id: str
    tool_name: str
    output: Any
    success: bool = True
    execution_time: float = 0.0
    error_message: str = ""

    def to_tool_message(self) -> dict:
        """Format as an OpenAI-compatible role='tool' message for the conversation context."""
        if isinstance(self.output, (dict, list)):
            content = json.dumps(self.output)
        else:
            content = str(self.output)

        # Defensive length bounding (max 10,000 chars per tool output)
        if len(content) > 10000:
            content = content[:10000] + " ... [output truncated]"

        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.tool_name,
            "content": content,
        }

    def to_dict(self) -> dict:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "success": self.success,
            "execution_time": round(self.execution_time, 3),
            "error_message": self.error_message,
        }


@dataclass
class ToolDefinition:
    """Registered tool contract."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    permission: PermissionLevel = PermissionLevel.READ_ONLY
    timeout_seconds: float = 5.0

    def to_openai_tool(self) -> dict:
        """Format as an OpenAI-compatible tool specification."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
