"""The run_readonly_command tool definition wired to the shared approval gate."""
from __future__ import annotations

import os
from typing import Any, Optional

from kivo.agent.approval_gate import ApprovalGate
from kivo.agent.terminal.command_executor import CommandExecutor
from kivo.agent.terminal.command_policy import (
    ALLOWLISTED_COMMANDS,
    CommandPolicyError,
    validate_terminal_command,
)
from kivo.agent.terminal.command_schemas import CommandExecutionRequest
from kivo.agent.terminal.path_validator import validate_working_directory
from kivo.agent.tool_schema import ExecutionGrant, PermissionLevel, ToolDefinition


def _log_audit(
    command: str,
    arguments: list[str],
    outcome: str,
    result_snippet: str,
    duration: float = 0.0,
) -> None:
    """Record terminal command lifecycle events in the central action policy audit log."""
    try:
        from core.action_policy import record_execution, Risk, Control

        safe_params = {
            "command": command,
            "arguments": " ".join(arguments)[:200],
        }
        record_execution(
            tool="run_readonly_command",
            action=command,
            parameters=safe_params,
            risk=Risk.MEDIUM,
            control=Control.CONFIRM,
            outcome=outcome,
            result=result_snippet[:200] if result_snippet else "",
            duration=duration,
        )
    except Exception:
        pass


def _build_execution_request(arguments: dict[str, Any]) -> CommandExecutionRequest:
    """Validate and normalize one terminal request.

    Called both before the approval UI is shown (by the common executor through
    the tool's ``preflight``) and again immediately before execution.
    """
    if not isinstance(arguments, dict):
        raise CommandPolicyError("Command arguments must be an object.")

    command = str(arguments.get("command", "")).strip().lower()
    raw_arguments = arguments.get("arguments", [])
    if raw_arguments is None:
        raw_arguments = []
    if not isinstance(raw_arguments, list):
        raise CommandPolicyError("Command arguments must be a list of strings.")
    if any(not isinstance(item, str) for item in raw_arguments):
        raise CommandPolicyError("Command arguments must be a list of strings.")

    command, args = validate_terminal_command(command, list(raw_arguments))

    raw_working_dir = str(arguments.get("working_directory", "")).strip()
    working_dir = str(
        validate_working_directory(raw_working_dir or str(os.path.expanduser("~")))
    )
    reason = str(arguments.get("reason", "")).strip() or f"Inspect system with '{command}'"

    return CommandExecutionRequest(
        command=command,
        arguments=args,
        working_directory=working_dir,
        reason=reason,
    )


def preflight_terminal_request(arguments: dict[str, Any]) -> None:
    """Fail before an approval card exists when the command is not allowed."""
    _build_execution_request(arguments)


def create_terminal_tool(
    approval_gate: ApprovalGate,
    executor: Optional[CommandExecutor] = None,
) -> ToolDefinition:
    """Create the run_readonly_command tool definition requiring explicit user approval."""
    cmd_executor = executor or CommandExecutor()

    async def handle_run_readonly_command(
        arguments: dict[str, Any],
        tool_call_id: str = "",
        execution_grant: Optional[ExecutionGrant] = None,
    ) -> dict[str, Any]:
        # 1. Validate before any approval path. Invalid commands never create a
        # pending request and never reach the executor.
        try:
            request = _build_execution_request(arguments)
        except Exception as exc:
            return {"error": f"Command validation failed: {exc}"}

        # 2. The common ToolExecutor owns approval. A valid grant for exactly
        # this tool call means approval already happened and must not be
        # requested again. Direct handler calls retain the internal fallback.
        if execution_grant is not None:
            if (
                not execution_grant.authorized
                or execution_grant.tool_name != "run_readonly_command"
                or (tool_call_id and execution_grant.tool_call_id != tool_call_id)
            ):
                return {
                    "error": (
                        "Authorization grant is missing, invalid, or bound to a "
                        "different tool call."
                    )
                }
        else:
            _log_audit(request.command, request.arguments, "approval_requested", request.reason)
            approved, status = await approval_gate.request_approval(
                tool_call_id=tool_call_id,
                tool_name="run_readonly_command",
                command=request.command,
                arguments=list(request.arguments),
                working_directory=request.working_directory,
                reason=request.reason,
            )
            if not approved:
                _log_audit(request.command, request.arguments, status, f"Approval was {status}")
                if status == "rejected":
                    return {
                        "error": (
                            f"The user explicitly rejected execution of the command "
                            f"'{request.command} {' '.join(request.arguments)}'. Do not "
                            f"attempt to run it again unless requested."
                        )
                    }
                if status == "expired":
                    return {
                        "error": (
                            f"Approval request for command '{request.command}' expired "
                            f"with no response from the user."
                        )
                    }
                return {
                    "error": (
                        f"Command approval request for '{request.command}' was {status}."
                    )
                }

        # 3. Revalidate immediately before execution. If the request changed
        # after approval (or the policy/cwd changed underneath us), refuse.
        try:
            final_request = _build_execution_request(arguments)
        except Exception as exc:
            return {"error": f"Command validation failed before execution: {exc}"}

        if final_request != request:
            return {
                "error": (
                    "Command request changed after approval; refusing to execute."
                )
            }

        result = await cmd_executor.execute(final_request)

        outcome = "executed" if result.success else "failed"
        snippet = result.stdout or result.stderr or result.error_message
        _log_audit(
            final_request.command,
            final_request.arguments,
            outcome,
            snippet,
            duration=result.execution_time,
        )

        return result.to_dict()

    allowed_list_str = ", ".join(sorted(ALLOWLISTED_COMMANDS))

    return ToolDefinition(
        name="run_readonly_command",
        description=(
            f"Run an approved, read-only Linux terminal inspection command from the allowlist "
            f"({allowed_list_str}). Requires explicit user approval on the HUD/Dashboard before running. "
            f"Output will be provided to help you answer the user's question."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": f"The read-only executable to run. Must be one of: {allowed_list_str}.",
                },
                "arguments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of command-line flags and parameters (e.g. ['-la', 'Downloads']).",
                },
                "working_directory": {
                    "type": "string",
                    "description": "Directory to run the command in (defaults to user home directory).",
                },
                "reason": {
                    "type": "string",
                    "description": "A clear, concise explanation shown on the user's approval banner explaining why this command is needed.",
                },
            },
            "required": ["command", "reason"],
            "additionalProperties": False,
        },
        handler=handle_run_readonly_command,
        permission=PermissionLevel.CONFIRMATION_REQUIRED,
        timeout_seconds=65.0,  # Accommodates 60s user approval + command timeout
        preflight=preflight_terminal_request,
    )
