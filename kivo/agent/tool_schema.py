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


DEFAULT_MAX_OUTPUT_CHARS = 10_000
MIN_MAX_OUTPUT_CHARS = 1_000
MAX_MAX_OUTPUT_CHARS = 200_000


def _bounded_message_limit(value: Any) -> int:
    """Clamp a per-tool output bound to a safe, finite range."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_OUTPUT_CHARS
    return max(MIN_MAX_OUTPUT_CHARS, min(limit, MAX_MAX_OUTPUT_CHARS))



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
    # Per-tool message bound, copied from the ToolDefinition that produced
    # this result. It lets a bounded file read return its full 32 KiB payload
    # without widening every other tool's output limit.
    max_message_chars: int = 10_000

    def to_tool_message(self) -> dict:
        """Format as an OpenAI-compatible role='tool' message for the conversation context."""
        if isinstance(self.output, (dict, list)):
            content = json.dumps(self.output)
        else:
            content = str(self.output)

        limit = _bounded_message_limit(self.max_message_chars)
        if len(content) > limit:
            content = content[:limit] + " ... [output truncated]"

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


@dataclass(frozen=True)
class LegacyActionBinding:
    """Explicit mapping from an agent tool to one allowlisted legacy action.

    A binding is code-owned metadata, never model-supplied. It records which
    legacy tool may be invoked, which action verbs are permitted, and how the
    action verb is selected from the modern tool arguments. Anything not named
    here is denied by the shared authorization policy.
    """

    tool_name: str
    allowed_actions: frozenset[str]
    action_argument: str = "action"
    default_action: str = ""
    fixed_action: str = ""


@dataclass(frozen=True)
class ExecutionGrant:
    """Capability proving that the shared execution path authorized a call.

    Handlers that adapt legacy actions refuse to run without a grant whose
    turn/tool-call binding matches the invocation.

    Read-only grants remain backward-compatible: ``approved`` is false and
    ``approval_fingerprint`` is empty.  A future mutation handler must require
    an explicit ``approved is True`` grant whose fingerprint matches its own
    ``ApprovalContext``.
    """

    turn_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    decision: str = "allow"
    authorized: bool = True
    approved: bool = False
    approval_fingerprint: str = ""


@dataclass
class ToolDefinition:
    """Registered tool contract."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    permission: PermissionLevel = PermissionLevel.READ_ONLY
    timeout_seconds: float = 5.0
    # Maximum characters returned to the model in the tool message. Most tools
    # keep the global 10,000-character bound; bounded file reads need headroom
    # for their explicitly truncated 32 KiB payload.
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    # Optional synchronous/asynchronous validation run before an approval is
    # requested. Used by the terminal tool so an invalid command never creates
    # an approval card the user could mistakenly approve.
    preflight: Optional[Callable[[dict[str, Any]], Any]] = None
    # Legacy actions treat strings as data, so only execution-capable tools
    # need the command-keyword scan.
    scan_for_forbidden_keywords: bool = True
    # Present only on tools that adapt a legacy ActionRegistry entry.
    legacy_binding: Optional[LegacyActionBinding] = None
    # A mutating tool is a filesystem mutation candidate. It must be
    # CONFIRMATION_REQUIRED, must not use a legacy action binding, and must
    # return a MutationPreflight from its preflight callback. No mutating tool
    # is registered in Stage 11.3.1a.
    mutating: bool = False
    # Optional per-request approval timeout. The gate still applies its own
    # global default when this is unset.
    approval_timeout_seconds: Optional[float] = None

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
