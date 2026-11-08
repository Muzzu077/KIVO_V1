"""Security policies, validation, and safeguards for tool execution."""
from __future__ import annotations

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


def _is_internal_key(key: Any) -> bool:
    """Keys beginning with an underscore are runtime bookkeeping, never input."""
    return isinstance(key, str) and key.startswith("_")


def _type_matches(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _validate_value(schema: dict[str, Any], value: Any, path: str, tool_name: str) -> None:
    """Validate one value against a deliberately small JSON-schema subset."""
    if not isinstance(schema, dict):
        return

    expected = str(schema.get("type", "")).lower()
    if expected and not _type_matches(expected, value):
        raise ToolPolicyError(
            f"Parameter '{path}' must be a "
            f"{'number' if expected in ('integer', 'number') else expected}, "
            f"got {type(value).__name__}."
        )

    allowed = schema.get("enum")
    if allowed is not None:
        if not isinstance(allowed, (list, tuple, set, frozenset)):
            raise ToolPolicyError(f"Tool '{tool_name}' has an invalid enum for '{path}'.")
        if value not in allowed:
            choices = ", ".join(str(choice) for choice in allowed)
            raise ToolPolicyError(
                f"Parameter '{path}' must be one of: {choices}."
            )

    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise ToolPolicyError(
                f"Parameter '{path}' must be at least {min_length} characters long."
            )
        if isinstance(max_length, int) and len(value) > max_length:
            raise ToolPolicyError(
                f"Parameter '{path}' must be at most {max_length} characters long."
            )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ToolPolicyError(f"Parameter '{path}' must be >= {minimum}.")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ToolPolicyError(f"Parameter '{path}' must be <= {maximum}.")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise ToolPolicyError(
                f"Parameter '{path}' must contain at least {min_items} item(s)."
            )
        if isinstance(max_items, int) and len(value) > max_items:
            raise ToolPolicyError(
                f"Parameter '{path}' must contain at most {max_items} item(s)."
            )
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_value(item_schema, item, f"{path}[{index}]", tool_name)

    if isinstance(value, dict) and str(schema.get("type", "")).lower() == "object":
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for req in required:
            if req not in value:
                raise ToolPolicyError(
                    f"Missing required parameter '{path}.{req}' for tool '{tool_name}'."
                )
        if schema.get("additionalProperties") is False:
            for key in value:
                if _is_internal_key(key):
                    raise ToolPolicyError(
                        f"Internal argument '{key}' is not accepted by tool "
                        f"'{tool_name}'."
                    )
                if key not in properties:
                    raise ToolPolicyError(
                        f"Unexpected argument '{key}' for tool '{tool_name}'."
                    )
        for key, item in value.items():
            if key in properties:
                _validate_value(
                    properties[key],
                    item,
                    f"{path}.{key}" if path else str(key),
                    tool_name,
                )


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
    raw_args = call.arguments
    if raw_args is None:
        args: dict[str, Any] = {}
    elif not isinstance(raw_args, dict):
        raise ToolPolicyError(f"Arguments for tool '{call.tool_name}' must be an object.")
    else:
        args = raw_args

    # Runtime bookkeeping keys can never be supplied by a model or a client.
    for key in args:
        if _is_internal_key(key):
            raise ToolPolicyError(
                f"Internal argument '{key}' is not accepted by tool '{call.tool_name}'."
            )

    # Check required fields
    for req in required_props:
        if req not in args:
            raise ToolPolicyError(f"Missing required parameter '{req}' for tool '{call.tool_name}'.")

    # Reject undeclared properties when the schema asks for a closed object.
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in expected_props:
                raise ToolPolicyError(
                    f"Unexpected argument '{key}' for tool '{call.tool_name}'."
                )

    # Type, enum, bounds, and nested validation
    for key, val in args.items():
        prop_schema = expected_props.get(key)
        if isinstance(prop_schema, dict):
            _validate_value(prop_schema, val, str(key), call.tool_name)

    # 4. Check for dangerous command injection in string arguments
    if getattr(tool_def, "scan_for_forbidden_keywords", True):
        for key, val in args.items():
            if isinstance(val, str):
                lower_val = val.lower()
                for forbidden in FORBIDDEN_KEYWORDS:
                    if forbidden in lower_val:
                        raise ToolPolicyError(
                            f"Argument '{key}' contains forbidden command keyword: '{forbidden}'."
                        )
