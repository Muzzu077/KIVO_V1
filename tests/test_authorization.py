"""Tests for the single shared authorization decision path used by agent tools."""
from __future__ import annotations

import unittest

from core.action_policy import (
    Control,
    Risk,
    clear_all_overrides,
    revoke_all,
    trust_session,
)
from kivo.agent.authorization import (
    AuthorizationAction,
    AuthorizationPolicy,
)
from kivo.agent.tool_schema import (
    LegacyActionBinding,
    PermissionLevel,
    ToolCall,
    ToolDefinition,
)


def _tool(
    name: str,
    permission: PermissionLevel,
    binding: LegacyActionBinding | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="test",
        parameters={"type": "object", "properties": {}},
        handler=lambda args: None,
        permission=permission,
        legacy_binding=binding,
    )


class AuthorizationPolicyTest(unittest.TestCase):
    def setUp(self):
        clear_all_overrides()
        revoke_all()
        trust_session(False)
        self.policy = AuthorizationPolicy()

    def tearDown(self):
        clear_all_overrides()
        revoke_all()
        trust_session(False)

    def _decide(self, tool: ToolDefinition, args: dict | None = None):
        return self.policy.decide(
            ToolCall(tool_call_id="call-1", tool_name=tool.name, arguments=args or {}),
            tool,
            turn_id="turn-1",
        )

    def test_read_only_modern_tool_is_allowed(self):
        decision = self._decide(_tool("get_time", PermissionLevel.READ_ONLY))
        self.assertEqual(decision.action, AuthorizationAction.ALLOW)
        self.assertEqual(decision.risk, Risk.SAFE)
        self.assertEqual(decision.control, Control.LOG)

    def test_confirmation_required_modern_tool_requires_approval(self):
        decision = self._decide(_tool("sensitive", PermissionLevel.CONFIRMATION_REQUIRED))
        self.assertEqual(decision.action, AuthorizationAction.REQUIRE_APPROVAL)

    def test_blocked_modern_tool_is_denied(self):
        decision = self._decide(_tool("danger", PermissionLevel.BLOCKED))
        self.assertEqual(decision.action, AuthorizationAction.DENY)

    def test_unknown_tool_is_denied_by_default(self):
        decision = self.policy.decide(
            ToolCall(tool_call_id="call-1", tool_name="unknown", arguments={}),
            None,
            turn_id="turn-1",
        )
        self.assertEqual(decision.action, AuthorizationAction.DENY)

    def test_legacy_log_action_is_allowed(self):
        binding = LegacyActionBinding(
            tool_name="web_search",
            allowed_actions=frozenset({"search", "news"}),
            action_argument="mode",
            default_action="search",
        )
        decision = self._decide(
            _tool("web_search", PermissionLevel.READ_ONLY, binding),
            {"query": "arch linux"},
        )
        self.assertEqual(decision.action, AuthorizationAction.ALLOW)
        self.assertEqual(decision.action_verb, "search")

    def test_legacy_undo_action_is_allowed(self):
        binding = LegacyActionBinding(
            tool_name="file_controller",
            allowed_actions=frozenset({"move"}),
            action_argument="action",
            default_action="move",
        )
        decision = self._decide(
            _tool("file_controller", PermissionLevel.READ_ONLY, binding),
            {"action": "move", "path": "a", "destination": "b"},
        )
        self.assertEqual(decision.action, AuthorizationAction.ALLOW)

    def test_legacy_confirm_action_requires_approval(self):
        binding = LegacyActionBinding(
            tool_name="file_controller",
            allowed_actions=frozenset({"delete"}),
            action_argument="action",
            default_action="delete",
        )
        decision = self._decide(
            _tool("file_controller", PermissionLevel.READ_ONLY, binding),
            {"action": "delete", "path": "a"},
        )
        self.assertEqual(decision.action, AuthorizationAction.REQUIRE_APPROVAL)

    def test_legacy_deny_action_is_denied(self):
        binding = LegacyActionBinding(
            tool_name="terminal_control",
            allowed_actions=frozenset({"run"}),
            action_argument="action",
            default_action="run",
        )
        decision = self._decide(
            _tool("terminal_control", PermissionLevel.READ_ONLY, binding),
            {"action": "run"},
        )
        self.assertEqual(decision.action, AuthorizationAction.DENY)

    def test_unlisted_legacy_action_is_denied(self):
        binding = LegacyActionBinding(
            tool_name="desktop_context",
            allowed_actions=frozenset({"list_windows"}),
            fixed_action="list_windows",
        )
        decision = self._decide(
            _tool("list_open_windows", PermissionLevel.READ_ONLY, binding),
            {"action": "screen_text"},
        )
        self.assertEqual(decision.action, AuthorizationAction.DENY)

    def test_trust_session_cannot_downgrade_agent_confirmation(self):
        trust_session(True)
        binding = LegacyActionBinding(
            tool_name="file_controller",
            allowed_actions=frozenset({"delete"}),
            action_argument="action",
            default_action="delete",
        )
        decision = self._decide(
            _tool("file_controller", PermissionLevel.READ_ONLY, binding),
            {"action": "delete", "path": "a"},
        )
        self.assertEqual(decision.action, AuthorizationAction.REQUIRE_APPROVAL)


if __name__ == "__main__":
    unittest.main()
