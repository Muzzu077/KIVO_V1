"""The run_readonly_command tool definition wired to the approval gate and executor."""
from __future__ import annotations

import os
from typing import Any, Optional

from kivo.agent.approval_gate import ApprovalGate
from kivo.agent.terminal.command_executor import CommandExecutor
from kivo.agent.terminal.command_policy import ALLOWLISTED_COMMANDS
from kivo.agent.terminal.command_schemas import CommandExecutionRequest
from kivo.agent.tool_schema import PermissionLevel, ToolDefinition


def _log_audit(command: str, arguments: list[str], outcome: str, result_snippet: str, duration: float = 0.0) -> None:
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


def create_terminal_tool(
    approval_gate: ApprovalGate,
    executor: Optional[CommandExecutor] = None,
) -> ToolDefinition:
    """Create the run_readonly_command tool definition requiring explicit user approval."""
    cmd_executor = executor or CommandExecutor()

    async def handle_run_readonly_command(arguments: dict[str, Any], tool_call_id: str = "") -> dict[str, Any]:
        command = str(arguments.get("command", "")).strip().lower()
        args_raw = arguments.get("arguments", [])
        args = [str(a) for a in args_raw] if isinstance(args_raw, list) else []
        working_dir = str(arguments.get("working_directory", "")).strip() or str(os.path.expanduser("~"))
        reason = str(arguments.get("reason", "")).strip() or f"Inspect system with '{command}'"

        # Record approval request in audit log
        _log_audit(command, args, "approval_requested", reason)

        # 1. Human-in-the-loop approval gate
        approved, status = await approval_gate.request_approval(
            tool_call_id=tool_call_id,
            tool_name="run_readonly_command",
            command=command,
            arguments=args,
            working_directory=working_dir,
            reason=reason,
        )

        if not approved:
            _log_audit(command, args, status, f"Approval was {status}")
            if status == "rejected":
                return {
                    "error": f"The user explicitly rejected execution of the command '{command} {' '.join(args)}'. Do not attempt to run it again unless requested."
                }
            elif status == "expired":
                return {
                    "error": f"Approval request for command '{command}' expired with no response from the user."
                }
            else:
                return {
                    "error": f"Command approval request for '{command}' was {status}."
                }

        # 2. Execute approved command
        exec_req = CommandExecutionRequest(
            command=command,
            arguments=args,
            working_directory=working_dir,
            reason=reason,
        )
        result = await cmd_executor.execute(exec_req)

        # Record execution outcome in audit log
        outcome = "executed" if result.success else "failed"
        snippet = result.stdout or result.stderr or result.error_message
        _log_audit(command, args, outcome, snippet, duration=result.execution_time)

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
        },
        handler=handle_run_readonly_command,
        permission=PermissionLevel.CONFIRMATION_REQUIRED,
        timeout_seconds=65.0,  # Accommodates 60s user approval + 5s command timeout
    )
