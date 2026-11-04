"""Security policies, validation, and safeguards for tool execution."""
from __future__ import annotations

import re
from typing import Any

from kivo.agent.tool_schema import PermissionLevel, ToolCall, ToolDefinition

MAX_OUTPUT_CHARS = 10000
MAX_AGENT_ROUNDS = 5
MAX_EXECUTION_TIMEOUT = 30.0

FORBIDDEN_KEYWORDS = frozenset({
    "eval",
    "exec",
    "os.system",
    "subprocess.popen",
    "rm -rf",
    ":(){ :|:& };:",
})


class ToolPolicyError(Exception):
    """Raised when a tool execution policy is violated."""


def validate_tool_call(call: ToolCall, tool_def: ToolDefinition, executed_ids: set[str]) -> None:
    """Validate that the requested tool call obeys all safety rules."""
    # 1. Deduplication / replay protection
    if call.tool_call_id in executed_ids:
        raise ToolPolicyError(f"Duplicate tool_call_id: '{call.tool_call_id}' was already executed.")

    # 2. Permission check
    if tool_def.permission == PermissionLevel.BLOCKED:
        raise ToolPolicyError(f"Tool '{call.tool_name}' is permanently blocked by security policy.")

    # 3. Argument schema validation
    schema = tool_def.parameters or {}
    expected_props = schema.get("properties", {})
    required_props = schema.get("required", [])
    args = call.arguments or {}

    # Check required fields
    for req in required_props:
        if req not in args:
            raise ToolPolicyError(f"Missing required parameter '{req}' for tool '{call.tool_name}'.")

    # Type check declared properties
    for key, val in args.items():
        if key in expected_props:
            prop_type = expected_props[key].get("type")
            if prop_type == "string" and not isinstance(val, str):
                raise ToolPolicyError(f"Parameter '{key}' must be a string, got {type(val).__name__}.")
            elif prop_type in ("integer", "number") and not isinstance(val, (int, float)):
                raise ToolPolicyError(f"Parameter '{key}' must be a number, got {type(val).__name__}.")
            elif prop_type == "boolean" and not isinstance(val, bool):
                raise ToolPolicyError(f"Parameter '{key}' must be a boolean, got {type(val).__name__}.")
            elif prop_type == "array" and not isinstance(val, list):
                raise ToolPolicyError(f"Parameter '{key}' must be an array, got {type(val).__name__}.")
            elif prop_type == "object" and not isinstance(val, dict):
                raise ToolPolicyError(f"Parameter '{key}' must be an object, got {type(val).__name__}.")

    # 4. Check for dangerous command injection in string arguments
    for key, val in args.items():
        if isinstance(val, str):
            lower_val = val.lower()
            for forbidden in FORBIDDEN_KEYWORDS:
                if forbidden in lower_val:
                    raise ToolPolicyError(f"Argument '{key}' contains forbidden command keyword: '{forbidden}'.")
