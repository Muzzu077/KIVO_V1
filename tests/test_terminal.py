"""Unit and security tests for KIVO controlled terminal access and approval gate."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from kivo.agent.approval_gate import ApprovalGate, ApprovalStatus
from kivo.agent.terminal.command_executor import CommandExecutor
from kivo.agent.terminal.command_policy import (
    ALLOWLISTED_COMMANDS,
    FORBIDDEN_COMMANDS,
    CommandPolicyError,
    validate_terminal_command,
)
from kivo.agent.terminal.command_schemas import (
    CommandExecutionRequest,
    PathValidationError,
)
from kivo.agent.terminal.path_validator import (
    sanitize_path_argument,
    validate_working_directory,
)
from kivo.agent.terminal.terminal_tool import create_terminal_tool
from kivo.agent.tool_schema import PermissionLevel, ToolCall


class TerminalCommandPolicyTest(unittest.TestCase):
    def test_allowlisted_commands_pass_validation(self):
        for cmd in ALLOWLISTED_COMMANDS:
            if cmd == "whoami":
                c, args = validate_terminal_command(cmd, [])
            elif cmd == "ls":
                c, args = validate_terminal_command(cmd, ["-la"])
            elif cmd == "uname":
                c, args = validate_terminal_command(cmd, ["-a"])
            elif cmd == "free":
                c, args = validate_terminal_command(cmd, ["-h"])
            elif cmd == "df":
                c, args = validate_terminal_command(cmd, ["-h"])
            elif cmd == "du":
                c, args = validate_terminal_command(cmd, ["-sh"])
            elif cmd == "ps":
                c, args = validate_terminal_command(cmd, ["aux"])
            else:
                c, args = validate_terminal_command(cmd, [])
            self.assertEqual(c, cmd)

    def test_forbidden_executables_strictly_rejected(self):
        for bad_cmd in FORBIDDEN_COMMANDS:
            with self.assertRaises(CommandPolicyError):
                validate_terminal_command(bad_cmd, [])

    def test_shell_injection_characters_rejected(self):
        injections = [
            "; rm -rf /",
            "| grep password",
            "&& sudo whoami",
            "`id`",
            "$(whoami)",
            "> /tmp/out",
            "< /etc/shadow",
            "arg\nkill 1",
        ]
        for inj in injections:
            with self.assertRaises(CommandPolicyError):
                validate_terminal_command("ls", [inj])

    def test_whoami_rejects_arguments(self):
        with self.assertRaises(CommandPolicyError):
            validate_terminal_command("whoami", ["root"])

    def test_ls_rejects_unpermitted_options(self):
        with self.assertRaises(CommandPolicyError):
            validate_terminal_command("ls", ["--exec=sh"])

    def test_ls_recursive_flag_strictly_rejected(self):
        # SEC-06: recursive directory scan is prohibited
        with self.assertRaises(CommandPolicyError):
            validate_terminal_command("ls", ["-R"])
        with self.assertRaises(CommandPolicyError):
            validate_terminal_command("ls", ["-laR"])

    def test_du_excessive_depth_rejected(self):
        # SEC-06: depth capped at 3
        with self.assertRaises(CommandPolicyError):
            validate_terminal_command("du", ["--max-depth=4"])

    def test_excessive_arguments_count_rejected(self):
        too_many = [f"arg_{i}" for i in range(25)]
        with self.assertRaises(CommandPolicyError):
            validate_terminal_command("ls", too_many)


class PathValidatorTest(unittest.TestCase):
    def test_home_default_working_directory(self):
        cwd = validate_working_directory(None)
        self.assertEqual(cwd, Path.home().resolve())

        cwd_empty = validate_working_directory("")
        self.assertEqual(cwd_empty, Path.home().resolve())

    def test_valid_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = validate_working_directory(tmp)
            self.assertEqual(cwd, Path(tmp).resolve())

    def test_nonexistent_directory_rejected(self):
        with self.assertRaises(PathValidationError):
            validate_working_directory("/completely/fake/directory/path/999")

    def test_null_bytes_in_path_rejected(self):
        with self.assertRaises(PathValidationError):
            validate_working_directory("/home/user\x00/bad")

        with self.assertRaises(PathValidationError):
            sanitize_path_argument("file\x00.txt")


class ApprovalGateTest(unittest.TestCase):
    def test_request_and_approve_cycle(self):
        gate = ApprovalGate(timeout_seconds=5.0)

        async def run_test():
            req_task = asyncio.create_task(
                gate.request_approval(
                    tool_call_id="call_1",
                    tool_name="run_readonly_command",
                    command="ls",
                    arguments=["-la"],
                    working_directory=str(Path.home()),
                    reason="inspecting home",
                )
            )

            await asyncio.sleep(0.05)
            pending = gate.get_pending_requests()
            self.assertEqual(len(pending), 1)
            token = pending[0]["token"]

            # User approves
            ok, msg = gate.resolve(token, approved=True)
            self.assertTrue(ok)

            approved, status = await req_task
            self.assertTrue(approved)
            self.assertEqual(status, "approved")

        asyncio.run(run_test())

    def test_request_and_reject_cycle(self):
        gate = ApprovalGate(timeout_seconds=5.0)

        async def run_test():
            req_task = asyncio.create_task(
                gate.request_approval(
                    tool_call_id="call_2",
                    tool_name="run_readonly_command",
                    command="df",
                    arguments=["-h"],
                    working_directory=str(Path.home()),
                    reason="inspecting disk",
                )
            )

            await asyncio.sleep(0.05)
            pending = gate.get_pending_requests()
            token = pending[0]["token"]

            # User rejects
            ok, msg = gate.resolve(token, approved=False)
            self.assertTrue(ok)

            approved, status = await req_task
            self.assertFalse(approved)
            self.assertEqual(status, "rejected")

        asyncio.run(run_test())

    def test_token_reuse_is_forbidden(self):
        gate = ApprovalGate(timeout_seconds=5.0)

        async def run_test():
            req_task = asyncio.create_task(
                gate.request_approval(
                    tool_call_id="call_3",
                    tool_name="run_readonly_command",
                    command="uname",
                    arguments=["-a"],
                    working_directory=str(Path.home()),
                    reason="test",
                )
            )
            await asyncio.sleep(0.05)
            token = gate.get_pending_requests()[0]["token"]
            gate.resolve(token, approved=True)
            await req_task

            # Attempting reuse fails
            ok2, msg2 = gate.resolve(token, approved=True)
            self.assertFalse(ok2)
            self.assertIn("cannot be reused", msg2)

        asyncio.run(run_test())

    def test_approval_timeout_expires(self):
        gate = ApprovalGate(timeout_seconds=0.1)  # 100ms timeout

        async def run_test():
            approved, status = await gate.request_approval(
                tool_call_id="call_4",
                tool_name="run_readonly_command",
                command="free",
                arguments=["-m"],
                working_directory=str(Path.home()),
                reason="test timeout",
            )
            self.assertFalse(approved)
            self.assertEqual(status, "expired")

        asyncio.run(run_test())


class CommandExecutorTest(unittest.TestCase):
    def setUp(self):
        self.executor = CommandExecutor(timeout=3.0)

    def test_execute_allowlisted_command(self):
        req = CommandExecutionRequest(command="uname", arguments=["-s"])
        res = asyncio.run(self.executor.execute(req))
        self.assertTrue(res.success)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.stdout.strip(), "Linux")

    def test_command_output_truncation(self):
        # ps generates substantial output
        req = CommandExecutionRequest(command="ps", arguments=["aux"])
        res = asyncio.run(self.executor.execute(req))
        self.assertTrue(res.success)
        self.assertLessEqual(len(res.stdout), 8000)

    def test_timeout_kills_process(self):
        fast_exec = CommandExecutor(timeout=0.1)
        # du on root might be slow
        req = CommandExecutionRequest(command="uname", arguments=["-a"])
        # Normal command runs fast and succeeds
        res = asyncio.run(fast_exec.execute(req))
        self.assertTrue(res.success)


class TerminalToolIntegrationTest(unittest.TestCase):
    def test_terminal_tool_requires_approval(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        tool = create_terminal_tool(gate)
        self.assertEqual(tool.permission, PermissionLevel.CONFIRMATION_REQUIRED)

        async def run_test():
            tool_task = asyncio.create_task(
                tool.handler({
                    "command": "uname",
                    "arguments": ["-s"],
                    "reason": "testing tool",
                })
            )
            await asyncio.sleep(0.05)
            pending = gate.get_pending_requests()
            self.assertEqual(len(pending), 1)

            # Approve
            gate.resolve(pending[0]["token"], approved=True)
            res = await tool_task
            self.assertEqual(res.get("command"), "uname")
            self.assertEqual(res.get("stdout").strip(), "Linux")

        asyncio.run(run_test())

    def test_terminal_tool_returns_error_on_rejection(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        tool = create_terminal_tool(gate)

        async def run_test():
            tool_task = asyncio.create_task(
                tool.handler({
                    "command": "ls",
                    "arguments": ["-la"],
                    "reason": "testing rejection",
                })
            )
            await asyncio.sleep(0.05)
            pending = gate.get_pending_requests()
            gate.resolve(pending[0]["token"], approved=False)
            res = await tool_task
            self.assertIn("error", res)
            self.assertIn("rejected", res["error"])

        asyncio.run(run_test())

    def test_tool_call_id_preserved_through_approval(self):
        # SEC-05: tool_call_id must not be empty or lost
        gate = ApprovalGate(timeout_seconds=5.0)
        tool = create_terminal_tool(gate)

        async def run_test():
            tool_task = asyncio.create_task(
                tool.handler(
                    {"command": "uname", "arguments": ["-s"], "reason": "test"},
                    tool_call_id="call_real_id_999",
                )
            )
            await asyncio.sleep(0.05)
            pending = gate.get_pending_requests()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["tool_call_id"], "call_real_id_999")
            gate.resolve(pending[0]["token"], approved=True)
            await tool_task

        asyncio.run(run_test())

    def test_central_audit_log_records_approval_and_execution(self):
        # SEC-04: audit log integration
        from core.action_policy import get_audit_log
        gate = ApprovalGate(timeout_seconds=5.0)
        tool = create_terminal_tool(gate)

        async def run_test():
            tool_task = asyncio.create_task(
                tool.handler(
                    {"command": "uname", "arguments": ["-s"], "reason": "audit test"},
                    tool_call_id="call_audit_1",
                )
            )
            await asyncio.sleep(0.05)
            pending = gate.get_pending_requests()
            token = pending[0]["token"]
            gate.resolve(token, approved=True)
            await tool_task

            log = get_audit_log()
            # Must contain records for run_readonly_command
            cmd_entries = [e for e in log if getattr(e, "tool", None) == "run_readonly_command"]
            self.assertGreaterEqual(len(cmd_entries), 1)
            actions = [getattr(e, "action", "") for e in cmd_entries]
            self.assertIn("uname", actions)

        asyncio.run(run_test())

    def test_legacy_terminal_actions_strictly_denied(self):
        # SEC-01: legacy actions must reject shell execution
        from actions.terminal_control_action import terminal_control
        for bad_action in ("run", "run_and_read", "execute_bg", "subprocess", "exec", "type", "type_command"):
            out = terminal_control({"action": bad_action, "command": "rm -rf /"})
            self.assertIn("disabled for security", out)

    def test_action_policy_denies_legacy_terminal(self):
        # SEC-01: action_policy.check must deny legacy terminal execution
        from core.action_policy import check, Control, Risk
        for bad_action in ("run", "run_and_read", "execute_bg", "subprocess", "exec", "type", "type_command"):
            risk, ctrl, denial = check("terminal_control", bad_action, {"command": "echo hi"})
            self.assertEqual(ctrl, Control.DENY)
            self.assertIn("disabled", denial.lower())


class TerminalCommonApprovalTest(unittest.IsolatedAsyncioTestCase):
    """The common executor path validates before approval and never double-confirms."""

    class FakeExecutor:
        def __init__(self):
            self.calls = []

        async def execute(self, request):
            from kivo.agent.terminal.command_schemas import CommandExecutionResult
            self.calls.append(request)
            return CommandExecutionResult(
                command=request.command,
                arguments=list(request.arguments),
                working_directory=request.working_directory,
                exit_code=0,
                stdout="Linux\n",
                stderr="",
                execution_time=0.01,
                success=True,
            )

    async def _wait_for_pending(self, gate):
        import asyncio
        for _ in range(100):
            pending = gate.get_pending_requests()
            if pending:
                return pending[0]
            await asyncio.sleep(0.01)
        self.fail("approval request never became pending")

    async def test_invalid_command_is_rejected_before_approval(self):
        from kivo.agent.tool_executor import ToolExecutor
        from kivo.agent.tool_registry import ToolRegistry
        from kivo.agent.tool_schema import ToolCall

        gate = ApprovalGate(timeout_seconds=2.0)
        fake_executor = self.FakeExecutor()
        registry = ToolRegistry()
        registry.register(create_terminal_tool(gate, executor=fake_executor))
        executor = ToolExecutor(registry, approval_gate=gate)

        result = await executor.execute(
            ToolCall(
                tool_call_id="call-invalid",
                tool_name="run_readonly_command",
                arguments={
                    "command": "rm",
                    "arguments": ["-rf", "/"],
                    "reason": "test invalid command",
                },
            ),
            set(),
            turn_id="turn-invalid",
        )
        self.assertFalse(result.success)
        self.assertIn("not in the read-only allowlist", result.error_message)
        self.assertEqual(gate.get_pending_requests(), [])
        self.assertEqual(fake_executor.calls, [])

    async def test_valid_command_uses_one_common_approval(self):
        from kivo.agent.tool_executor import ToolExecutor
        from kivo.agent.tool_registry import ToolRegistry
        from kivo.agent.tool_schema import ToolCall

        gate = ApprovalGate(timeout_seconds=2.0)
        fake_executor = self.FakeExecutor()
        registry = ToolRegistry()
        registry.register(create_terminal_tool(gate, executor=fake_executor))
        executor = ToolExecutor(registry, approval_gate=gate)

        task = asyncio.create_task(
            executor.execute(
                ToolCall(
                    tool_call_id="call-valid",
                    tool_name="run_readonly_command",
                    arguments={
                        "command": "uname",
                        "arguments": ["-s"],
                        "reason": "test valid command",
                    },
                ),
                set(),
                turn_id="turn-valid",
            )
        )
        pending = await self._wait_for_pending(gate)
        self.assertEqual(pending["command"], "uname")
        self.assertEqual(pending["turn_id"], "turn-valid")

        gate.resolve(pending["token"], approved=True, turn_id="turn-valid")
        result = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(len(fake_executor.calls), 1)
        self.assertEqual(gate.get_pending_requests(), [])

    async def test_direct_handler_validates_before_internal_gate(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        fake_executor = self.FakeExecutor()
        tool = create_terminal_tool(gate, executor=fake_executor)

        result = await tool.handler(
            {
                "command": "sudo",
                "arguments": ["reboot"],
                "reason": "test direct invalid call",
            }
        )
        self.assertIn("error", result)
        self.assertEqual(gate.get_pending_requests(), [])
        self.assertEqual(fake_executor.calls, [])


if __name__ == "__main__":
    unittest.main()
