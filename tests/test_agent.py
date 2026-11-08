"""Unit tests for the KIVO agent runtime, tool registry, executor, and built-in tools."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from kivo.agent.agent_runtime import AgentRuntime
from kivo.agent.tool_executor import ToolExecutor
from kivo.agent.tool_policy import ToolPolicyError, validate_tool_call
from kivo.agent.tool_registry import ToolRegistry
from kivo.agent.tool_schema import (
    PermissionLevel,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from kivo.agent.tools.system_info import SYSTEM_OVERVIEW_TOOL, handle_get_system_overview
from kivo.agent.tools.time_info import CURRENT_TIME_TOOL, handle_get_current_time
from kivo.conversation.providers.base import LLMProvider


class ToolSchemaAndRegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()

    def test_tool_registration_and_lookup(self):
        tool = ToolDefinition(
            name="dummy_tool",
            description="A dummy test tool",
            parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
            handler=lambda args: f"echo: {args.get('msg')}",
        )
        self.registry.register(tool)
        self.assertTrue(self.registry.has_tool("dummy_tool"))
        self.assertEqual(self.registry.get_tool("dummy_tool").name, "dummy_tool")

        openai_tools = self.registry.get_openai_tools()
        self.assertEqual(len(openai_tools), 1)
        self.assertEqual(openai_tools[0]["type"], "function")
        self.assertEqual(openai_tools[0]["function"]["name"], "dummy_tool")

        self.registry.unregister("dummy_tool")
        self.assertFalse(self.registry.has_tool("dummy_tool"))

    def test_tool_result_to_message_formatting(self):
        res = ToolResult(
            tool_call_id="call_99",
            tool_name="dummy_tool",
            output={"result": "ok", "value": 42},
            success=True,
            execution_time=0.12,
        )
        msg = res.to_tool_message()
        self.assertEqual(msg["role"], "tool")
        self.assertEqual(msg["tool_call_id"], "call_99")
        self.assertEqual(msg["name"], "dummy_tool")
        self.assertIn('"ok"', msg["content"])


class BuiltinToolsTest(unittest.TestCase):
    def test_get_current_time_returns_expected_fields(self):
        res = handle_get_current_time({})
        self.assertIn("time", res)
        self.assertIn("date", res)
        self.assertIn("day_of_week", res)
        self.assertIn("timezone", res)
        self.assertIn("iso_timestamp", res)
        self.assertTrue(len(res["timezone"]) > 0)

    def test_get_system_overview_returns_hardware_telemetry(self):
        res = handle_get_system_overview({})
        self.assertIn("os", res)
        self.assertIn("kernel", res)
        self.assertIn("cpu", res)
        self.assertIn("memory", res)
        self.assertIn("disk_root", res)
        self.assertTrue(res["cpu"]["physical_cores"] >= 1)
        self.assertTrue(res["memory"]["total_gb"] > 0)
        self.assertTrue(res["disk_root"]["total_gb"] > 0)


class ToolPolicyAndExecutorTest(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register(CURRENT_TIME_TOOL)
        self.registry.register(SYSTEM_OVERVIEW_TOOL)
        self.executor = ToolExecutor(self.registry)

    def test_unknown_tool_returns_safe_error(self):
        call = ToolCall(tool_call_id="c_unk", tool_name="unknown_delete_tool", arguments={})
        res = asyncio.run(self.executor.execute(call, set()))
        self.assertFalse(res.success)
        self.assertIn("Unknown tool", res.error_message)

    def test_non_object_arguments_are_rejected(self):
        call = ToolCall(
            tool_call_id="c_bad_args",
            tool_name="get_current_time",
            arguments=[],  # type: ignore[arg-type]
        )
        res = asyncio.run(self.executor.execute(call, set()))
        self.assertFalse(res.success)
        self.assertIn("must be an object", res.error_message)

    def test_tool_call_id_deduplication_prevents_replay(self):
        executed = {"call_already_run"}
        call = ToolCall(tool_call_id="call_already_run", tool_name="get_current_time", arguments={})
        res = asyncio.run(self.executor.execute(call, executed))
        self.assertFalse(res.success)
        self.assertIn("Duplicate", res.error_message)

    def test_blocked_permission_tool_rejected(self):
        blocked_tool = ToolDefinition(
            name="dangerous_action",
            description="Danger",
            parameters={"type": "object"},
            handler=lambda args: None,
            permission=PermissionLevel.BLOCKED,
        )
        self.registry.register(blocked_tool)
        call = ToolCall(tool_call_id="c_block", tool_name="dangerous_action", arguments={})
        res = asyncio.run(self.executor.execute(call, set()))
        self.assertFalse(res.success)
        self.assertIn("permanently blocked", res.error_message)

    def test_tool_argument_type_validation(self):
        tool = ToolDefinition(
            name="typed_tool",
            description="test",
            parameters={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
            handler=lambda args: args["count"] * 2,
        )
        self.registry.register(tool)

        # Missing required parameter
        call_missing = ToolCall(tool_call_id="c_m", tool_name="typed_tool", arguments={})
        res_m = asyncio.run(self.executor.execute(call_missing, set()))
        self.assertFalse(res_m.success)
        self.assertIn("Missing required parameter", res_m.error_message)

        # Invalid type
        call_bad_type = ToolCall(tool_call_id="c_t", tool_name="typed_tool", arguments={"count": "not-an-int"})
        res_t = asyncio.run(self.executor.execute(call_bad_type, set()))
        self.assertFalse(res_t.success)
        self.assertIn("must be a number", res_t.error_message)

    def test_tool_execution_timeout(self):
        def slow_handler(args):
            time.sleep(2.0)
            return "done"

        slow_tool = ToolDefinition(
            name="slow_tool",
            description="slow",
            parameters={"type": "object"},
            handler=slow_handler,
            timeout_seconds=0.1,  # 100ms cutoff
        )
        self.registry.register(slow_tool)
        call = ToolCall(tool_call_id="c_slow", tool_name="slow_tool", arguments={})
        res = asyncio.run(self.executor.execute(call, set()))
        self.assertFalse(res.success)
        self.assertIn("timed out", res.error_message)


class AgentRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register(CURRENT_TIME_TOOL)
        self.registry.register(SYSTEM_OVERVIEW_TOOL)
        self.mock_provider = MagicMock(spec=LLMProvider)
        self.runtime = AgentRuntime(provider=self.mock_provider, registry=self.registry, max_rounds=5)

    def test_text_only_response_without_tools_returns_immediately(self):
        async def fake_stream(messages, tools=None, **kwargs):
            yield "Hello, "
            yield "I am KIVO."

        self.mock_provider.stream = fake_stream

        deltas = []
        messages = [{"role": "user", "content": "Hi"}]

        text = asyncio.run(self.runtime.execute_turn(
            messages,
            on_delta=lambda d, acc: deltas.append(d),
        ))
        self.assertEqual(text, "Hello, I am KIVO.")
        self.assertEqual(deltas, ["Hello, ", "I am KIVO."])
        # Did not append tool calls
        self.assertEqual(len(messages), 1)

    def test_multi_round_tool_execution_cycle(self):
        # Round 1: Model requests get_current_time
        # Round 2: Model receives tool output and answers user
        round_counter = [0]

        async def fake_stream(messages, tools=None, **kwargs):
            round_counter[0] += 1
            if round_counter[0] == 1:
                # Return tool call
                yield {
                    "type": "tool_calls",
                    "tool_calls": [
                        ToolCall(tool_call_id="call_t1", tool_name="get_current_time", arguments={})
                    ],
                }
            else:
                # Final answer
                yield "The current time is 7:30 PM."

        self.mock_provider.stream = fake_stream

        tools_started = []
        tools_completed = []
        messages = [{"role": "user", "content": "What time is it?"}]

        text = asyncio.run(self.runtime.execute_turn(
            messages,
            on_tool_started=lambda name, cid: tools_started.append(name),
            on_tool_completed=lambda res: tools_completed.append(res.tool_name),
        ))

        self.assertEqual(text, "The current time is 7:30 PM.")
        self.assertEqual(tools_started, ["get_current_time"])
        self.assertEqual(tools_completed, ["get_current_time"])
        # Messages should have: user turn, assistant with tool_call, tool result
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertIn("tool_calls", messages[1])
        self.assertEqual(messages[2]["role"], "tool")
        self.assertEqual(messages[2]["tool_call_id"], "call_t1")

    def test_max_rounds_cutoff_prevents_infinite_loops(self):
        # Model stubbornly keeps returning tool calls forever
        async def endless_tool_stream(messages, tools=None, **kwargs):
            yield {
                "type": "tool_calls",
                "tool_calls": [
                    ToolCall(tool_call_id=f"c_{time.time()}", tool_name="get_current_time", arguments={})
                ],
            }

        self.mock_provider.stream = endless_tool_stream
        messages = [{"role": "user", "content": "Loop"}]

        # Should cutoff at max 5 rounds without hanging
        t0 = time.monotonic()
        text = asyncio.run(self.runtime.execute_turn(messages))
        self.assertLess(time.monotonic() - t0, 5.0)
        # Should have executed max 5 rounds of tool turns
        self.assertLessEqual(len(messages), 1 + (5 * 2))


class CommonApprovalEnforcementTest(unittest.IsolatedAsyncioTestCase):
    """CONFIRMATION_REQUIRED must be enforced by ToolExecutor, not each handler."""

    async def _wait_for_pending(self, gate):
        for _ in range(100):
            pending = gate.get_pending_requests()
            if pending:
                return pending[0]
            await asyncio.sleep(0.01)
        self.fail("approval request never became pending")

    async def test_confirmation_required_tool_requests_bound_approval(self):
        from kivo.agent.approval_gate import ApprovalGate
        from kivo.agent.authorization import AuthorizationPolicy

        ran = []

        async def handler(arguments, tool_call_id="", execution_grant=None):
            ran.append((arguments, tool_call_id, execution_grant))
            return {"ok": True}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="sensitive_tool",
                description="test",
                parameters={"type": "object", "properties": {}},
                handler=handler,
                permission=PermissionLevel.CONFIRMATION_REQUIRED,
                timeout_seconds=5.0,
            )
        )
        gate = ApprovalGate(timeout_seconds=2.0)
        executor = ToolExecutor(
            registry,
            approval_gate=gate,
            authorization_policy=AuthorizationPolicy(),
        )

        task = asyncio.create_task(
            executor.execute(
                ToolCall(
                    tool_call_id="call-bound-1",
                    tool_name="sensitive_tool",
                    arguments={},
                ),
                set(),
                turn_id="turn-bound-1",
            )
        )
        pending = await self._wait_for_pending(gate)
        self.assertEqual(pending["turn_id"], "turn-bound-1")
        self.assertEqual(pending["tool_call_id"], "call-bound-1")
        self.assertEqual(pending["tool_name"], "sensitive_tool")

        self.assertTrue(
            gate.resolve(pending["token"], approved=True, turn_id="turn-bound-1")[0]
        )
        result = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(len(ran), 1)
        grant = ran[0][2]
        self.assertEqual(grant.turn_id, "turn-bound-1")
        self.assertEqual(grant.tool_call_id, "call-bound-1")
        self.assertTrue(grant.authorized)

    async def test_confirmation_required_tool_fails_closed_without_gate(self):
        ran = []

        def handler(arguments):
            ran.append(arguments)
            return {"ok": True}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="sensitive_tool",
                description="test",
                parameters={"type": "object", "properties": {}},
                handler=handler,
                permission=PermissionLevel.CONFIRMATION_REQUIRED,
            )
        )
        executor = ToolExecutor(registry)
        result = await executor.execute(
            ToolCall(
                tool_call_id="call-no-gate",
                tool_name="sensitive_tool",
                arguments={},
            ),
            set(),
            turn_id="turn-no-gate",
        )
        self.assertFalse(result.success)
        self.assertIn("approval", result.error_message.lower())
        self.assertEqual(ran, [])

    async def test_rejection_prevents_execution(self):
        from kivo.agent.approval_gate import ApprovalGate

        ran = []

        async def handler(arguments):
            ran.append(arguments)
            return {"ok": True}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="sensitive_tool",
                description="test",
                parameters={"type": "object", "properties": {}},
                handler=handler,
                permission=PermissionLevel.CONFIRMATION_REQUIRED,
            )
        )
        gate = ApprovalGate(timeout_seconds=2.0)
        executor = ToolExecutor(registry, approval_gate=gate)
        task = asyncio.create_task(
            executor.execute(
                ToolCall(
                    tool_call_id="call-reject",
                    tool_name="sensitive_tool",
                    arguments={},
                ),
                set(),
                turn_id="turn-reject",
            )
        )
        pending = await self._wait_for_pending(gate)
        gate.resolve(pending["token"], approved=False, turn_id="turn-reject")

        result = await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(result.success)
        self.assertIn("rejected", result.error_message.lower())
        self.assertEqual(ran, [])

    async def test_synchronous_mutating_handler_is_refused(self):
        def handler(arguments):
            return {"ok": True}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="sync_sensitive",
                description="test",
                parameters={"type": "object", "properties": {}},
                handler=handler,
                permission=PermissionLevel.CONFIRMATION_REQUIRED,
            )
        )
        executor = ToolExecutor(registry)
        result = await executor.execute(
            ToolCall(
                tool_call_id="call-sync",
                tool_name="sync_sensitive",
                arguments={},
            ),
            set(),
            turn_id="turn-sync",
        )
        self.assertFalse(result.success)
        self.assertIn("synchronous", result.error_message.lower())

    async def test_audit_records_turn_and_call_without_arguments(self):
        from core.action_policy import get_audit_log
        from kivo.agent.authorization import AuthorizationPolicy

        async def handler(arguments):
            return {"ok": True}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="audited_tool",
                description="test",
                parameters={
                    "type": "object",
                    "properties": {"secret": {"type": "string"}},
                },
                handler=handler,
                permission=PermissionLevel.READ_ONLY,
            )
        )
        executor = ToolExecutor(
            registry,
            authorization_policy=AuthorizationPolicy(),
        )
        result = await executor.execute(
            ToolCall(
                tool_call_id="call-audit-1",
                tool_name="audited_tool",
                arguments={"secret": "do-not-log-this"},
            ),
            set(),
            turn_id="turn-audit-1",
        )
        self.assertTrue(result.success)

        entries = [
            entry for entry in get_audit_log(limit=20)
            if entry.tool == "audited_tool"
        ]
        self.assertTrue(entries)
        entry = entries[0]
        self.assertEqual(entry.turn_id, "turn-audit-1")
        self.assertEqual(entry.tool_call_id, "call-audit-1")
        self.assertEqual(entry.decision, "allow")
        self.assertNotIn("do-not-log-this", str(entry.parameters))


if __name__ == "__main__":
    unittest.main()
