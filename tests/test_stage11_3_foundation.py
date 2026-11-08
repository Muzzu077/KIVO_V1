"""Stage 11.3.1a mutation security foundation tests.

These tests deliberately register no production mutation tool.  They exercise
the shared approval-context, grant, budget, executor, gate, worker, and audit
contracts with local test-only tools.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

from core.action_policy import (
    Control,
    Risk,
    clear_audit_log,
    get_audit_log,
)
from kivo.agent.approval_context import (
    ApprovalContext,
    ApprovalContextError,
    ApprovalDisplay,
    MutationBinding,
    MutationPreflight,
    canonicalize_relative_path,
    validate_mutation_grant,
)
from kivo.agent.approval_gate import (
    ApprovalGate,
    ApprovalKind,
    normalize_approval_details,
)
from kivo.agent.authorization import (
    AuthorizationAction,
    AuthorizationDecision,
    AuthorizationPolicy,
)
from kivo.agent.mutation_budget import MutationBudget
from kivo.agent.mutation_audit import record_mutation_event
from kivo.agent.tool_executor import ToolExecutor
from kivo.agent.tool_registry import ToolRegistry
from kivo.agent.tool_schema import (
    ExecutionGrant,
    LegacyActionBinding,
    PermissionLevel,
    ToolCall,
    ToolDefinition,
)
from kivo.files.service import FileIntelligenceError
from kivo.files.tools import BoundedFileWorker
from kivo.conversation.context_manager import ContextManager
from kivo.conversation.conversation_manager import ConversationManager
from kivo.conversation.providers.base import LLMProvider


def _digest(content: bytes = b"hello") -> str:
    return hashlib.sha256(content).hexdigest()


def _binding(**overrides) -> MutationBinding:
    values = {
        "workspace_id": "demo",
        "relative_path": "docs/notes.txt",
        "content_digest": _digest(),
        "byte_count": 5,
        "target_state": "absent",
        "root_identity": "root-identity-1",
        "parent_identity": "parent-identity-1",
    }
    values.update(overrides)
    return MutationBinding(**values)


def _display(**overrides) -> ApprovalDisplay:
    values = {
        "title": "Create text file",
        "summary": "Create docs/notes.txt in workspace demo.",
    }
    values.update(overrides)
    return ApprovalDisplay(**values)


def _context(**overrides) -> ApprovalContext:
    values = {
        "operation_kind": "create_text_file",
        "tool_name": "create_text_file",
        "turn_id": "turn-1",
        "tool_call_id": "call-1",
        "display": _display(),
        "binding": _binding(),
    }
    values.update(overrides)
    return ApprovalContext(**values)


def _preflight(**overrides) -> MutationPreflight:
    values = {
        "operation_kind": "create_text_file",
        "display": _display(),
        "binding": _binding(),
    }
    values.update(overrides)
    return MutationPreflight(**values)


def _mutation_tool(
    *,
    name: str = "create_text_file",
    preflight=lambda arguments: _preflight(),
    handler=None,
    permission: PermissionLevel = PermissionLevel.CONFIRMATION_REQUIRED,
    legacy_binding=None,
    approval_timeout_seconds=None,
) -> ToolDefinition:
    async def default_handler(
        arguments,
        tool_call_id="",
        execution_grant=None,
        turn_id="",
        approval_context=None,
    ):
        return {"ok": True, "status": "created"}

    return ToolDefinition(
        name=name,
        description="test-only mutation",
        parameters={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "relative_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["workspace_id", "relative_path", "content"],
            "additionalProperties": False,
        },
        handler=handler or default_handler,
        permission=permission,
        mutating=True,
        legacy_binding=legacy_binding,
        preflight=preflight,
        scan_for_forbidden_keywords=False,
        timeout_seconds=5.0,
        approval_timeout_seconds=approval_timeout_seconds,
    )


def _mutation_call(call_id: str = "call-1", tool: str = "create_text_file") -> ToolCall:
    return ToolCall(
        tool_call_id=call_id,
        tool_name=tool,
        arguments={
            "workspace_id": "demo",
            "relative_path": "docs/notes.txt",
            "content": "hello",
        },
    )


async def _wait_for_pending(gate: ApprovalGate, expected: int = 1) -> dict:
    for _ in range(200):
        pending = gate.get_pending_requests()
        if len(pending) >= expected:
            return pending[-1]
        await asyncio.sleep(0.005)
    raise AssertionError("approval request never became pending")


class ApprovalContextTest(unittest.TestCase):
    def test_relative_path_canonicalization(self):
        self.assertEqual(canonicalize_relative_path(""), "")
        self.assertEqual(canonicalize_relative_path("."), "")
        self.assertEqual(canonicalize_relative_path("./docs//notes.txt"), "docs/notes.txt")
        self.assertEqual(canonicalize_relative_path("docs/./notes.txt"), "docs/notes.txt")
        for invalid in ("/ etc/passwd", "../escape", "docs/../escape", "a\\b", "a\x00b"):
            with self.assertRaises(ApprovalContextError, msg=invalid):
                canonicalize_relative_path(invalid)

    def test_fingerprint_is_stable_and_deterministic(self):
        first = _context()
        second = _context(binding=_binding())
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.fingerprint, hashlib.sha256(
            first._canonical_fingerprint_json().encode("utf-8")
        ).hexdigest())

    def test_fingerprint_binds_every_security_field(self):
        base = _context().fingerprint
        variants = [
            _context(operation_kind="write_text_file"),
            _context(tool_name="write_text_file"),
            _context(turn_id="turn-2"),
            _context(tool_call_id="call-2"),
            _context(binding=_binding(workspace_id="other")),
            _context(binding=_binding(relative_path="docs/other.txt")),
            _context(binding=_binding(content_digest=_digest(b"other"))),
            _context(binding=_binding(byte_count=6)),
            _context(binding=_binding(target_state="present")),
            _context(binding=_binding(root_identity="root-identity-2")),
            _context(binding=_binding(parent_identity="parent-identity-2")),
        ]
        fingerprints = {base}
        for variant in variants:
            self.assertNotEqual(base, variant.fingerprint)
            fingerprints.add(variant.fingerprint)
        self.assertEqual(len(fingerprints), len(variants) + 1)

    def test_public_serialization_hides_private_binding(self):
        context = _context()
        public = context.to_dict()
        serialized = json.dumps(public, sort_keys=True)
        for private_value in (
            "root-identity-1",
            "parent-identity-1",
            "root_identity",
            "parent_identity",
        ):
            self.assertNotIn(private_value, serialized)
        self.assertNotIn("binding", public)
        self.assertIn("fingerprint", public)
        self.assertEqual(public["operation_kind"], "create_text_file")

    def test_nested_binding_and_display_are_immutable(self):
        context = _context()
        with self.assertRaises(FrozenInstanceError):
            context.binding.relative_path = "other.txt"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            context.display.title = "other"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            context.turn_id = "turn-2"  # type: ignore[misc]

    def test_fingerprint_mismatch_is_rejected(self):
        context = _context()
        self.assertTrue(context.verify_fingerprint(context.fingerprint))
        self.assertFalse(context.verify_fingerprint("0" * 64))
        self.assertFalse(context.verify_fingerprint(None))

    def test_malformed_binding_values_are_rejected(self):
        bad_values = (
            {"workspace_id": ""},
            {"workspace_id": "../demo"},
            {"content_digest": "not-a-digest"},
            {"content_digest": "A" * 64},
            {"byte_count": -1},
            {"byte_count": True},
            {"target_state": "Bad State"},
            {"root_identity": "has space"},
            {"parent_identity": ""},
        )
        for bad in bad_values:
            with self.assertRaises(ApprovalContextError, msg=str(bad)):
                _binding(**bad)

    def test_serialized_size_is_strictly_bounded(self):
        with patch(
            "kivo.agent.approval_context.MAX_APPROVAL_CONTEXT_BYTES",
            16,
        ):
            with self.assertRaises(ApprovalContextError):
                _context()

    def test_preflight_from_value_accepts_context_and_wrapper(self):
        context = _context()
        direct = MutationPreflight.from_value(context)
        self.assertEqual(direct.operation_kind, context.operation_kind)
        self.assertEqual(direct.display, context.display)
        self.assertEqual(direct.binding, context.binding)

        wrapped = MutationPreflight.from_value({"approval_context": context})
        self.assertEqual(wrapped.binding, context.binding)

        direct_mapping = MutationPreflight.from_value(
            {
                "operation_kind": context.operation_kind,
                "display": context.display,
                "binding": context.binding,
            }
        )
        self.assertEqual(direct_mapping.display.title, context.display.title)

        with_extra = MutationPreflight.from_value(
            {
                "approval_context": context,
                "ok": True,
                "preview": "ignored metadata",
            }
        )
        self.assertEqual(with_extra.binding, context.binding)

        for malformed in (
            {"operation_kind": context.operation_kind, "display": context.display},
            {"approval_context": "not-a-context"},
            "not-a-preflight",
        ):
            with self.assertRaises(ApprovalContextError, msg=repr(malformed)):
                MutationPreflight.from_value(malformed)

    def test_grant_validation_accepts_only_complete_mutation_grants(self):
        context = _context()
        grant = ExecutionGrant(
            turn_id=context.turn_id,
            tool_call_id=context.tool_call_id,
            tool_name=context.tool_name,
            decision="require_approval",
            authorized=True,
            approved=True,
            approval_fingerprint=context.fingerprint,
        )
        self.assertIsNone(
            validate_mutation_grant(
                grant,
                tool_name=context.tool_name,
                turn_id=context.turn_id,
                tool_call_id=context.tool_call_id,
                context=context,
            )
        )
        forged = [
            ExecutionGrant(),
            ExecutionGrant(
                turn_id=context.turn_id,
                tool_call_id=context.tool_call_id,
                tool_name=context.tool_name,
                decision="allow",
                approved=True,
                approval_fingerprint=context.fingerprint,
            ),
            ExecutionGrant(
                turn_id=context.turn_id,
                tool_call_id=context.tool_call_id,
                tool_name=context.tool_name,
                decision="require_approval",
                authorized=False,
                approved=True,
                approval_fingerprint=context.fingerprint,
            ),
            ExecutionGrant(
                turn_id=context.turn_id,
                tool_call_id=context.tool_call_id,
                tool_name=context.tool_name,
                decision="require_approval",
                approved=False,
                approval_fingerprint=context.fingerprint,
            ),
            ExecutionGrant(
                turn_id="turn-2",
                tool_call_id=context.tool_call_id,
                tool_name=context.tool_name,
                decision="require_approval",
                approved=True,
                approval_fingerprint=context.fingerprint,
            ),
            ExecutionGrant(
                turn_id=context.turn_id,
                tool_call_id="call-2",
                tool_name=context.tool_name,
                decision="require_approval",
                approved=True,
                approval_fingerprint=context.fingerprint,
            ),
            ExecutionGrant(
                turn_id=context.turn_id,
                tool_call_id=context.tool_call_id,
                tool_name="other_tool",
                decision="require_approval",
                approved=True,
                approval_fingerprint=context.fingerprint,
            ),
            ExecutionGrant(
                turn_id=context.turn_id,
                tool_call_id=context.tool_call_id,
                tool_name=context.tool_name,
                decision="require_approval",
                approved=True,
                approval_fingerprint="1" * 64,
            ),
        ]
        for candidate in forged:
            self.assertIsNotNone(
                validate_mutation_grant(
                    candidate,
                    tool_name=context.tool_name,
                    turn_id=context.turn_id,
                    tool_call_id=context.tool_call_id,
                    context=context,
                )
            )


class MutationBudgetTest(unittest.TestCase):
    def test_concurrent_claims_have_exactly_one_winner(self):
        budget = MutationBudget()
        barrier = threading.Barrier(32)
        results: list[bool] = []
        lock = threading.Lock()

        def claim() -> None:
            barrier.wait(timeout=2.0)
            won = budget.claim("turn-race")
            with lock:
                results.append(won)

        threads = [threading.Thread(target=claim) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)
        self.assertEqual(sum(1 for item in results if item), 1)
        self.assertEqual(len(results), 32)

    def test_turn_isolation_and_missing_turn(self):
        budget = MutationBudget()
        self.assertFalse(budget.claim(""))
        self.assertTrue(budget.claim("turn-a"))
        self.assertFalse(budget.claim("turn-a"))
        self.assertTrue(budget.claim("turn-b"))
        self.assertTrue(budget.is_claimed("turn-a"))


class ApprovalGateExtensionTest(unittest.IsolatedAsyncioTestCase):
    async def _start(self, gate: ApprovalGate, **kwargs):
        task = asyncio.create_task(
            gate.request_approval(
                tool_call_id=kwargs.pop("tool_call_id", "call-1"),
                tool_name="test_tool",
                command="test_action",
                arguments=[],
                working_directory="",
                reason="testing",
                turn_id=kwargs.pop("turn_id", "turn-1"),
                **kwargs,
            )
        )
        pending = await _wait_for_pending(gate)
        return task, pending

    async def test_details_and_kind_are_bounded_and_visible(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        task, pending = await self._start(
            gate,
            approval_kind=ApprovalKind.FILESYSTEM_MUTATION,
            details={
                "operation_kind": "create_text_file",
                "workspace_id": "demo",
                "relative_path": "docs/notes.txt",
                "byte_count": 5,
            },
        )
        self.assertEqual(pending["approval_kind"], "filesystem_mutation")
        self.assertEqual(pending["details"]["workspace_id"], "demo")
        self.assertEqual(pending["details"]["byte_count"], 5)
        gate.resolve(
            pending["token"],
            approved=True,
            turn_id=pending["turn_id"],
            tool_call_id=pending["tool_call_id"],
        )
        approved, _ = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(approved)

    async def test_malformed_details_never_become_pending(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        malformed = [
            {"nested": {"secret": "value"}},
            {"list_value": [1, 2]},
            {"UPPER": "value"},
            {"too_long": "x" * 600},
            {f"k{index}": index for index in range(20)},
        ]
        for details in malformed:
            approved, status = await gate.request_approval(
                tool_call_id="call-bad",
                tool_name="test_tool",
                command="test_action",
                arguments=[],
                working_directory="",
                reason="testing",
                turn_id="turn-bad",
                details=details,
            )
            self.assertFalse(approved)
            self.assertEqual(status, "invalid_approval_request")
            self.assertEqual(gate.get_pending_requests(), [])

    async def test_required_tool_call_identity_is_mandatory(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        task, pending = await self._start(
            gate,
            tool_call_id="call-required",
            require_tool_call_id=True,
        )
        self.assertTrue(pending["requires_tool_call_id"])

        resolved, _ = gate.resolve(
            pending["token"], approved=True, turn_id="turn-1"
        )
        self.assertFalse(resolved)
        resolved, _ = gate.resolve(
            pending["token"],
            approved=True,
            turn_id="turn-1",
            tool_call_id="call-other",
        )
        self.assertFalse(resolved)
        self.assertEqual(len(gate.get_pending_requests()), 1)

        resolved, _ = gate.resolve(
            pending["token"],
            approved=True,
            turn_id="turn-1",
            tool_call_id="call-required",
        )
        self.assertTrue(resolved)
        approved, _ = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(approved)

        replay, message = gate.resolve(
            pending["token"],
            approved=True,
            turn_id="turn-1",
            tool_call_id="call-required",
        )
        self.assertFalse(replay)
        self.assertIn("reused", message.lower())

    async def test_per_request_timeout_overrides_default(self):
        gate = ApprovalGate(timeout_seconds=10.0)
        task, _ = await self._start(gate, timeout_seconds=0.05)
        approved, status = await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(approved)
        self.assertEqual(status, "expired")
        self.assertEqual(gate.get_pending_requests(), [])

    async def test_resolve_rejects_non_boolean_decision(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        task, pending = await self._start(gate)
        resolved, message = gate.resolve(
            pending["token"],
            "yes",  # type: ignore[arg-type]
            turn_id=pending["turn_id"],
            tool_call_id=pending["tool_call_id"],
        )
        self.assertFalse(resolved)
        self.assertIn("boolean", message.lower())
        self.assertEqual(len(gate.get_pending_requests()), 1)
        self.assertTrue(
            gate.resolve(
                pending["token"],
                True,
                turn_id=pending["turn_id"],
                tool_call_id=pending["tool_call_id"],
            )[0]
        )
        approved, _ = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(approved)

    async def test_terminal_approval_remains_backward_compatible(self):
        gate = ApprovalGate(timeout_seconds=2.0)
        task, pending = await self._start(gate)
        resolved, _ = gate.resolve(
            pending["token"],
            approved=True,
            turn_id=pending["turn_id"],
        )
        self.assertTrue(resolved)
        approved, status = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(approved)
        self.assertEqual(status, "approved")

    def test_detail_normalization_is_deterministic(self):
        first = normalize_approval_details({"b": 2, "a": "one"})
        second = normalize_approval_details({"a": "one", "b": 2})
        self.assertEqual(first, second)
        self.assertEqual(first[0][0], "a")


class ToolExecutorMutationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_audit_log()
        self.gate = ApprovalGate(timeout_seconds=2.0)
        self.registry = ToolRegistry()

    def _executor(self, policy=None, budget=None) -> ToolExecutor:
        return ToolExecutor(
            self.registry,
            approval_gate=self.gate,
            authorization_policy=policy or AuthorizationPolicy(),
            mutation_budget=budget,
        )

    async def test_preflight_result_is_captured_and_bound(self):
        seen: list[dict] = []
        captured: dict = {}

        def preflight(arguments):
            seen.append(arguments)
            return _preflight()

        async def handler(
            arguments,
            tool_call_id="",
            execution_grant=None,
            turn_id="",
            approval_context=None,
        ):
            captured.update(
                arguments=arguments,
                grant=execution_grant,
                context=approval_context,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
            return {"ok": True, "status": "created"}

        self.registry.register(_mutation_tool(preflight=preflight, handler=handler))
        executor = self._executor()
        task = asyncio.create_task(
            executor.execute(_mutation_call(), set(), turn_id="turn-1")
        )
        pending = await _wait_for_pending(self.gate)
        self.assertTrue(pending["requires_tool_call_id"])
        self.assertEqual(pending["approval_kind"], "filesystem_mutation")
        self.assertNotIn("root-identity-1", json.dumps(pending))
        self.assertNotIn("parent-identity-1", json.dumps(pending))
        self.assertNotIn("hello", json.dumps(pending))

        self.assertTrue(
            self.gate.resolve(
                pending["token"],
                approved=True,
                turn_id="turn-1",
                tool_call_id="call-1",
            )[0]
        )
        result = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(seen, [_mutation_call().arguments])
        context = captured["context"]
        self.assertIsInstance(context, ApprovalContext)
        self.assertEqual(context.turn_id, "turn-1")
        self.assertEqual(context.tool_call_id, "call-1")
        self.assertEqual(context.binding.workspace_id, "demo")
        grant = captured["grant"]
        self.assertTrue(grant.approved)
        self.assertEqual(grant.decision, "require_approval")
        self.assertEqual(grant.approval_fingerprint, context.fingerprint)
        self.assertIsNone(
            validate_mutation_grant(
                grant,
                tool_name="create_text_file",
                turn_id="turn-1",
                tool_call_id="call-1",
                context=context,
            )
        )

    async def test_preflight_failure_creates_no_approval_card(self):
        ran: list[bool] = []

        def preflight(arguments):
            raise ValueError("binding refused")

        async def handler(arguments, **kwargs):
            ran.append(True)
            return {"ok": True}

        self.registry.register(_mutation_tool(preflight=preflight, handler=handler))
        executor = self._executor()
        result = await executor.execute(_mutation_call(), set(), turn_id="turn-1")
        self.assertFalse(result.success)
        self.assertIn("binding refused", result.error_message)
        self.assertEqual(self.gate.get_pending_requests(), [])
        self.assertEqual(ran, [])

    async def test_missing_preflight_binding_fails_closed(self):
        self.registry.register(
            _mutation_tool(preflight=lambda arguments: None)
        )
        executor = self._executor()
        result = await executor.execute(_mutation_call(), set(), turn_id="turn-1")
        self.assertFalse(result.success)
        self.assertIn("valid approval context", result.error_message.lower())
        self.assertEqual(self.gate.get_pending_requests(), [])

    async def test_authorization_allow_is_rejected_for_mutations(self):
        class AllowAllPolicy:
            def decide(self, call, tool_def, turn_id=""):
                return AuthorizationDecision(
                    action=AuthorizationAction.ALLOW,
                    risk=Risk.SAFE,
                    control=Control.LOG,
                    action_verb="*",
                )

        ran: list[bool] = []

        async def handler(arguments, **kwargs):
            ran.append(True)
            return {"ok": True}

        self.registry.register(_mutation_tool(handler=handler))
        executor = self._executor(policy=AllowAllPolicy())
        result = await executor.execute(_mutation_call(), set(), turn_id="turn-1")
        self.assertFalse(result.success)
        self.assertIn("not classified", result.error_message.lower())
        self.assertEqual(self.gate.get_pending_requests(), [])
        self.assertEqual(ran, [])

        second = await executor.execute(
            _mutation_call(call_id="call-2"), set(), turn_id="turn-1"
        )
        self.assertFalse(second.success)
        self.assertIn("single mutation attempt", second.error_message.lower())

    async def test_mutating_read_only_tool_is_denied_by_policy(self):
        self.registry.register(
            _mutation_tool(permission=PermissionLevel.READ_ONLY)
        )
        executor = self._executor()
        result = await executor.execute(_mutation_call(), set(), turn_id="turn-1")
        self.assertFalse(result.success)
        self.assertEqual(self.gate.get_pending_requests(), [])

    async def test_mutating_legacy_binding_is_denied(self):
        binding = LegacyActionBinding(
            tool_name="file_controller",
            allowed_actions=frozenset({"create_file"}),
            action_argument="action",
            default_action="create_file",
        )
        self.registry.register(_mutation_tool(legacy_binding=binding))
        executor = self._executor()
        result = await executor.execute(_mutation_call(), set(), turn_id="turn-1")
        self.assertFalse(result.success)
        self.assertEqual(self.gate.get_pending_requests(), [])

    async def test_model_supplied_fingerprint_is_never_accepted(self):
        self.registry.register(_mutation_tool())
        executor = self._executor()
        call = ToolCall(
            tool_call_id="call-forged",
            tool_name="create_text_file",
            arguments={
                "workspace_id": "demo",
                "relative_path": "docs/notes.txt",
                "content": "hello",
                "approval_fingerprint": "0" * 64,
            },
        )
        result = await executor.execute(call, set(), turn_id="turn-1")
        self.assertFalse(result.success)
        self.assertIn("model-supplied", result.error_message.lower())
        self.assertEqual(self.gate.get_pending_requests(), [])

    async def test_ok_false_is_reported_as_failure(self):
        async def handler(arguments, **kwargs):
            return {"ok": False, "error": "create refused"}

        self.registry.register(_mutation_tool(handler=handler))
        executor = self._executor()
        task = asyncio.create_task(
            executor.execute(_mutation_call(), set(), turn_id="turn-1")
        )
        pending = await _wait_for_pending(self.gate)
        self.gate.resolve(
            pending["token"],
            approved=True,
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        result = await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(result.success)
        self.assertIn("create refused", result.error_message)

        entries = [
            entry for entry in get_audit_log(limit=20)
            if entry.tool == "create_text_file"
        ]
        self.assertTrue(any(entry.outcome == "failed" for entry in entries))

    async def test_mutation_requires_explicit_ok_true(self):
        async def handler(arguments, **kwargs):
            return {"status": "created"}

        self.registry.register(_mutation_tool(handler=handler))
        executor = self._executor()
        task = asyncio.create_task(
            executor.execute(_mutation_call(), set(), turn_id="turn-1")
        )
        pending = await _wait_for_pending(self.gate)
        self.gate.resolve(
            pending["token"],
            approved=True,
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        result = await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(result.success)
        self.assertIn("ok: true", result.error_message.lower())

    async def test_rejected_attempt_consumes_turn_budget(self):
        self.registry.register(_mutation_tool())
        executor = self._executor()
        task = asyncio.create_task(
            executor.execute(_mutation_call(), set(), turn_id="turn-1")
        )
        pending = await _wait_for_pending(self.gate)
        self.gate.resolve(
            pending["token"],
            approved=False,
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        first = await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(first.success)

        second = await executor.execute(
            _mutation_call(call_id="call-2"), set(), turn_id="turn-1"
        )
        self.assertFalse(second.success)
        self.assertIn("single mutation attempt", second.error_message.lower())

    async def test_expired_attempt_consumes_turn_budget(self):
        self.registry.register(
            _mutation_tool(approval_timeout_seconds=0.05)
        )
        executor = self._executor()
        first = await asyncio.wait_for(
            executor.execute(_mutation_call(), set(), turn_id="turn-1"),
            timeout=2.0,
        )
        self.assertFalse(first.success)
        self.assertIn("expired", first.error_message.lower())

        second = await executor.execute(
            _mutation_call(call_id="call-2"), set(), turn_id="turn-1"
        )
        self.assertFalse(second.success)
        self.assertIn("single mutation attempt", second.error_message.lower())

    async def test_cancelled_attempt_consumes_turn_budget(self):
        self.registry.register(_mutation_tool())
        executor = self._executor()
        task = asyncio.create_task(
            executor.execute(_mutation_call(), set(), turn_id="turn-1")
        )
        await _wait_for_pending(self.gate)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.gate.cancel_all()

        entries = [
            entry for entry in get_audit_log(limit=20)
            if entry.tool == "create_text_file"
        ]
        self.assertTrue(any(entry.outcome == "cancelled" for entry in entries))

        second = await executor.execute(
            _mutation_call(call_id="call-2"), set(), turn_id="turn-1"
        )
        self.assertFalse(second.success)
        self.assertIn("single mutation attempt", second.error_message.lower())

    async def test_new_turn_gets_fresh_budget_and_reads_still_work(self):
        async def read_handler(arguments):
            return {"ok": True, "status": "read"}

        self.registry.register(_mutation_tool())
        self.registry.register(
            ToolDefinition(
                name="read_tool",
                description="read-only",
                parameters={"type": "object", "properties": {}},
                handler=read_handler,
                permission=PermissionLevel.READ_ONLY,
            )
        )
        executor = self._executor()
        task = asyncio.create_task(
            executor.execute(_mutation_call(), set(), turn_id="turn-1")
        )
        pending = await _wait_for_pending(self.gate)
        self.gate.resolve(
            pending["token"],
            approved=False,
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        await asyncio.wait_for(task, timeout=2.0)

        read_result = await executor.execute(
            ToolCall(tool_call_id="read-1", tool_name="read_tool", arguments={}),
            set(),
            turn_id="turn-1",
        )
        self.assertTrue(read_result.success, read_result.error_message)

        second_task = asyncio.create_task(
            executor.execute(
                _mutation_call(call_id="call-2"), set(), turn_id="turn-2"
            )
        )
        pending = await _wait_for_pending(self.gate)
        self.gate.resolve(
            pending["token"],
            approved=True,
            turn_id="turn-2",
            tool_call_id="call-2",
        )
        second = await asyncio.wait_for(second_task, timeout=2.0)
        self.assertTrue(second.success, second.error_message)

    async def test_audit_intent_failure_prevents_execution(self):
        ran: list[bool] = []

        async def handler(arguments, **kwargs):
            ran.append(True)
            return {"ok": True}

        self.registry.register(_mutation_tool(handler=handler))
        executor = self._executor()
        with patch(
            "core.action_policy.record_execution",
            side_effect=RuntimeError("audit unavailable"),
        ):
            task = asyncio.create_task(
                executor.execute(_mutation_call(), set(), turn_id="turn-1")
            )
            pending = await _wait_for_pending(self.gate)
            self.gate.resolve(
                pending["token"],
                approved=True,
                turn_id="turn-1",
                tool_call_id="call-1",
            )
            result = await asyncio.wait_for(task, timeout=2.0)
        self.assertFalse(result.success)
        self.assertIn("audit intent", result.error_message.lower())
        self.assertEqual(ran, [])

    async def test_outcome_audit_failure_reports_committed_state_and_warning(self):
        async def handler(arguments, **kwargs):
            return {"ok": True, "status": "created", "relative_path": "docs/notes.txt"}

        self.registry.register(_mutation_tool(handler=handler))
        executor = self._executor()
        calls = {"count": 0}

        def audit_sink(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("audit outcome unavailable")
            return None

        with patch("core.action_policy.record_execution", side_effect=audit_sink):
            task = asyncio.create_task(
                executor.execute(_mutation_call(), set(), turn_id="turn-1")
            )
            pending = await _wait_for_pending(self.gate)
            self.gate.resolve(
                pending["token"],
                approved=True,
                turn_id="turn-1",
                tool_call_id="call-1",
            )
            result = await asyncio.wait_for(task, timeout=2.0)

        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.output["status"], "created")
        self.assertIn("audit_warning", result.output)
        self.assertIn("completed", result.output["audit_warning"].lower())

    async def test_mutation_audit_contains_only_allowed_metadata(self):
        async def handler(arguments, **kwargs):
            return {"ok": True, "status": "created"}

        self.registry.register(_mutation_tool(handler=handler))
        executor = self._executor()
        secret_call = ToolCall(
            tool_call_id="call-1",
            tool_name="create_text_file",
            arguments={
                "workspace_id": "demo",
                "relative_path": "docs/notes.txt",
                "content": "TOP-SECRET file content",
            },
        )
        task = asyncio.create_task(
            executor.execute(secret_call, set(), turn_id="turn-1")
        )
        pending = await _wait_for_pending(self.gate)
        self.gate.resolve(
            pending["token"],
            approved=True,
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        result = await asyncio.wait_for(task, timeout=2.0)
        self.assertTrue(result.success, result.error_message)

        entries = [
            entry for entry in get_audit_log(limit=20)
            if entry.tool == "create_text_file"
        ]
        self.assertTrue(entries)
        allowed_keys = {"workspace_id", "relative_path", "content_digest", "byte_count"}
        serialized = ""
        for entry in entries:
            self.assertTrue(set(entry.parameters).issubset(allowed_keys))
            self.assertNotIn("root-identity-1", str(entry.parameters))
            self.assertNotIn("parent-identity-1", str(entry.parameters))
            self.assertNotIn("TOP-SECRET", str(entry.parameters))
            self.assertNotIn("approval_fingerprint", str(entry.parameters))
            serialized += str(entry.parameters)
        self.assertNotIn("TOP-SECRET", serialized)

    def test_audit_event_rejects_non_metadata_values(self):
        clear_audit_log()
        base = {
            "tool_name": "create_text_file",
            "operation_kind": "create_text_file",
            "status": "failed",
            "turn_id": "turn-1",
            "tool_call_id": "call-1",
        }
        for invalid in (
            {"workspace_id": "../escape"},
            {"workspace_id": "x" * 65},
            {"relative_path": "../escape"},
            {"relative_path": "/absolute/path"},
            {"content_digest": "A" * 64},
            {"byte_count": -1},
            {"byte_count": True},
        ):
            self.assertFalse(
                record_mutation_event(**base, **invalid),
                msg=repr(invalid),
            )
        self.assertEqual(get_audit_log(limit=20), [])


class WorkerLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_rejects_new_work_and_is_idempotent(self):
        worker = BoundedFileWorker()
        self.assertTrue(worker.close())
        self.assertTrue(worker.is_closed)
        self.assertTrue(worker.is_shutdown)
        with self.assertRaises(FileIntelligenceError) as raised:
            await worker.run(lambda cancel_event=None: "nope")
        self.assertEqual(raised.exception.code, "worker_closed")
        self.assertTrue(worker.close())

    async def test_close_cancels_running_work_cooperatively(self):
        worker = BoundedFileWorker()
        started = threading.Event()

        def slow(cancel_event=None):
            started.set()
            while not cancel_event.is_set():
                time.sleep(0.005)
            return "stopped"

        task = asyncio.create_task(worker.run(slow))
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(started.is_set())

        started_at = time.monotonic()
        self.assertTrue(worker.close(timeout_seconds=1.0))
        elapsed = time.monotonic() - started_at
        self.assertLess(elapsed, 2.0)

        try:
            result = await asyncio.wait_for(task, timeout=2.0)
        except FileIntelligenceError as exc:
            self.assertIn(exc.code, {"shutdown", "worker_closed"})
        else:
            self.assertEqual(result, "stopped")

    async def test_close_is_bounded_for_non_cooperative_work(self):
        worker = BoundedFileWorker()
        started = threading.Event()
        release = threading.Event()

        def stubborn(cancel_event=None):
            started.set()
            release.wait(timeout=2.0)
            return "finished-late"

        task = asyncio.create_task(worker.run(stubborn))
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(started.is_set())

        started_at = time.monotonic()
        stopped = worker.close(timeout_seconds=0.05)
        elapsed = time.monotonic() - started_at
        self.assertFalse(stopped)
        self.assertLess(elapsed, 1.0)
        with self.assertRaises(FileIntelligenceError) as raised:
            await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(raised.exception.code, "shutdown")

        release.set()
        for _ in range(200):
            if worker.shutdown_complete:
                break
            await asyncio.sleep(0.005)
        self.assertTrue(worker.shutdown_complete)

    async def test_cancel_active_keeps_worker_available(self):
        worker = BoundedFileWorker()
        started = threading.Event()

        def slow(cancel_event=None):
            started.set()
            while not cancel_event.is_set():
                time.sleep(0.005)
            return "stopped"

        task = asyncio.create_task(worker.run(slow))
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(worker.cancel_active())
        self.assertEqual(await asyncio.wait_for(task, timeout=2.0), "stopped")

        for _ in range(200):
            try:
                result = await worker.run(lambda cancel_event=None: "after")
            except FileIntelligenceError as exc:
                self.assertEqual(exc.code, "busy")
                await asyncio.sleep(0.005)
                continue
            self.assertEqual(result, "after")
            break
        else:
            self.fail("worker never became available after cancel_active")
        worker.close()

    async def test_concurrent_submit_and_close_never_hang(self):
        worker = BoundedFileWorker()

        async def one(index: int):
            try:
                return await worker.run(
                    lambda cancel_event=None, value=index: value
                )
            except FileIntelligenceError as exc:
                return exc

        tasks = [asyncio.create_task(one(index)) for index in range(10)]
        closed = await asyncio.to_thread(worker.close, 1.0)
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=3.0)
        self.assertTrue(closed)
        for result in results:
            self.assertTrue(
                isinstance(result, FileIntelligenceError)
                or isinstance(result, int)
            )
        with self.assertRaises(FileIntelligenceError):
            await worker.run(lambda cancel_event=None: "late")


class RegistryReadOnlyInvariantTest(unittest.TestCase):
    def test_default_registry_registers_no_mutations(self):
        from kivo.agent import create_default_tool_registry

        registry = create_default_tool_registry()
        self.assertFalse(registry.has_mutating_tools())
        self.assertEqual(registry.list_mutating_tool_names(), [])
        registry.assert_read_only()
        for name in registry.list_tool_names():
            tool = registry.get_tool(name)
            self.assertFalse(tool.mutating)
            self.assertEqual(tool.permission, PermissionLevel.READ_ONLY)

    def test_registry_assert_read_only_rejects_marked_tools(self):
        registry = ToolRegistry()
        registry.register(_mutation_tool(name="future_mutation"))
        self.assertTrue(registry.has_mutating_tools())
        self.assertEqual(registry.list_mutating_tool_names(), ["future_mutation"])
        with self.assertRaises(RuntimeError):
            registry.assert_read_only()


class ConversationWorkerOwnershipTest(unittest.TestCase):
    def test_provided_worker_is_cancelled_but_not_closed(self):
        class FakeWorker:
            def __init__(self):
                self.cancelled = 0
                self.closed = 0

            def cancel_active(self):
                self.cancelled += 1
                return True

            def close(self):
                self.closed += 1
                return True

        provider = MagicMock(spec=LLMProvider)
        worker = FakeWorker()
        manager = ConversationManager(
            context_manager=ContextManager(),
            provider=provider,
            tool_registry=ToolRegistry(),
            file_worker=worker,  # type: ignore[arg-type]
        )
        manager.close()
        self.assertEqual(worker.cancelled, 1)
        self.assertEqual(worker.closed, 0)


if __name__ == "__main__":
    unittest.main()
