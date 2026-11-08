"""Single authorization decision path for modern tools and adapted legacy actions.

Every tool invocation - whether it is implemented directly for the agent runtime
or adapted from the legacy ``ActionRegistry`` - is classified here before any
approval prompt or handler call. The policy is intentionally small and
default-deny:

    ALLOW             the call may execute
    REQUIRE_APPROVAL  a human must approve this exact turn/tool call
    DENY              the call must not execute

Legacy control values map deliberately:

    LOG / UNDO  -> ALLOW
    CONFIRM     -> REQUIRE_APPROVAL
    DENY        -> DENY

Session trust (``core.action_policy.trust_session``) is never allowed to
downgrade an agent confirmation. The legacy lookup is invoked with
``respect_trust=False`` so a relaxed demo session cannot silently turn a
confirmation-gated action into an automatic one.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from core.action_policy import Control, Risk, check as legacy_check
from kivo.agent.tool_schema import (
    LegacyActionBinding,
    PermissionLevel,
    ToolCall,
    ToolDefinition,
)


class AuthorizationAction(str, Enum):
    """What the shared authorization policy permits for a tool call."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class AuthorizationDecision:
    """Immutable result of a single authorization evaluation."""

    action: AuthorizationAction
    risk: Risk
    control: Control
    reason: str = ""
    action_verb: str = ""


_CONTROL_TO_ACTION = {
    Control.LOG: AuthorizationAction.ALLOW,
    Control.UNDO: AuthorizationAction.ALLOW,
    Control.CONFIRM: AuthorizationAction.REQUIRE_APPROVAL,
    Control.DENY: AuthorizationAction.DENY,
}


def _deny(reason: str, action_verb: str = "") -> AuthorizationDecision:
    return AuthorizationDecision(
        action=AuthorizationAction.DENY,
        risk=Risk.CRITICAL,
        control=Control.DENY,
        reason=reason,
        action_verb=action_verb,
    )


class AuthorizationPolicy:
    """Authoritative allow / approval / deny decisions for agent tools."""

    def decide(
        self,
        call: ToolCall,
        tool_def: Optional[ToolDefinition],
        turn_id: str = "",
    ) -> AuthorizationDecision:
        """Classify one tool call. Unknown or unclassified tools fail closed."""
        if tool_def is None:
            return _deny(
                f"Tool '{call.tool_name}' is not registered and has no authorization policy."
            )

        # A blocked tool can never be rescued by a legacy binding or by trust.
        if tool_def.permission == PermissionLevel.BLOCKED:
            return _deny(
                f"Tool '{call.tool_name}' is permanently blocked by security policy."
            )

        # Mutating tools have a deliberately narrower policy surface than
        # read-only modern tools. A mutation can never use a legacy action
        # adapter, and it can never be classified as automatic/read-only.
        if getattr(tool_def, "mutating", False):
            if tool_def.legacy_binding is not None:
                return _deny(
                    f"Mutating tool '{call.tool_name}' cannot use a legacy action binding."
                )
            if tool_def.permission != PermissionLevel.CONFIRMATION_REQUIRED:
                return _deny(
                    f"Mutating tool '{call.tool_name}' must be explicitly "
                    f"confirmation-required."
                )
            if not isinstance(turn_id, str) or not turn_id.strip():
                return _deny(
                    f"Mutating tool '{call.tool_name}' requires a conversation turn."
                )
            return AuthorizationDecision(
                action=AuthorizationAction.REQUIRE_APPROVAL,
                risk=Risk.HIGH,
                control=Control.CONFIRM,
                reason=(
                    f"Mutating tool '{call.tool_name}' requires explicit user "
                    f"approval for exactly one turn and tool call."
                ),
                action_verb="*",
            )

        binding = tool_def.legacy_binding
        if binding is not None:
            return self._decide_legacy(call, tool_def, binding, turn_id)

        if tool_def.permission == PermissionLevel.CONFIRMATION_REQUIRED:
            return AuthorizationDecision(
                action=AuthorizationAction.REQUIRE_APPROVAL,
                risk=Risk.HIGH,
                control=Control.CONFIRM,
                reason=f"Tool '{call.tool_name}' requires explicit user approval.",
                action_verb="*",
            )

        if tool_def.permission == PermissionLevel.READ_ONLY:
            return AuthorizationDecision(
                action=AuthorizationAction.ALLOW,
                risk=Risk.SAFE,
                control=Control.LOG,
                action_verb="*",
            )

        return _deny(
            f"Tool '{call.tool_name}' has an unrecognized permission classification."
        )

    def _decide_legacy(
        self,
        call: ToolCall,
        tool_def: ToolDefinition,
        binding: LegacyActionBinding,
        turn_id: str,
    ) -> AuthorizationDecision:
        allowed = binding.allowed_actions
        if not binding.tool_name or not allowed:
            return _deny(
                f"Tool '{call.tool_name}' has an invalid legacy action binding."
            )

        arguments: dict[str, Any] = call.arguments or {}
        provided = arguments.get(binding.action_argument) if binding.action_argument else None
        if provided is not None and not isinstance(provided, str):
            return _deny(
                f"Argument '{binding.action_argument}' for tool '{call.tool_name}' "
                f"must be a string action verb."
            )

        requested = provided.strip().lower() if isinstance(provided, str) else ""
        if requested:
            # A model-supplied action must be named in the explicit allowlist,
            # even when the tool has a fixed action. This is what keeps a
            # metadata-only window tool from being steered into OCR or input.
            if requested not in allowed:
                return _deny(
                    f"Action '{requested}' is not in the explicit allowlist for "
                    f"tool '{call.tool_name}'.",
                    action_verb=requested,
                )
            action_verb = requested
        elif binding.fixed_action:
            action_verb = binding.fixed_action
        elif binding.default_action:
            action_verb = binding.default_action
        else:
            return _deny(
                f"Tool '{call.tool_name}' did not select an allowlisted action."
            )

        if action_verb not in allowed:
            return _deny(
                f"Action '{action_verb}' is not in the explicit allowlist for "
                f"tool '{call.tool_name}'.",
                action_verb=action_verb,
            )

        policy_parameters = dict(arguments)
        if binding.action_argument:
            policy_parameters[binding.action_argument] = action_verb

        try:
            risk, control, denial = legacy_check(
                binding.tool_name,
                action_verb,
                policy_parameters,
                respect_trust=False,
                turn_id=turn_id,
                tool_call_id=call.tool_call_id,
            )
        except Exception as exc:
            return _deny(
                f"Legacy authorization policy failed for tool '{call.tool_name}': {exc}",
                action_verb=action_verb,
            )

        action = _CONTROL_TO_ACTION.get(control)
        if action is None:
            return _deny(
                f"Legacy action '{binding.tool_name}:{action_verb}' returned an "
                f"unrecognized control value.",
                action_verb=action_verb,
            )

        if action == AuthorizationAction.DENY:
            return AuthorizationDecision(
                action=action,
                risk=risk,
                control=control,
                reason=denial or (
                    f"Legacy action '{binding.tool_name}:{action_verb}' is denied "
                    f"by policy."
                ),
                action_verb=action_verb,
            )

        return AuthorizationDecision(
            action=action,
            risk=risk,
            control=control,
            reason="",
            action_verb=action_verb,
        )


__all__ = [
    "AuthorizationAction",
    "AuthorizationDecision",
    "AuthorizationPolicy",
]
