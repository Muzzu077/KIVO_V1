"""Safe execution runner for agent tools with shared authorization and timeouts.

The executor is the single place where a tool call becomes executable.  For a
future filesystem mutation it additionally:

* claims the turn's one mutation attempt before preflight;
* requires a ``MutationPreflight`` result and binds it to the trusted turn and
  tool-call identifiers;
* obtains explicit approval through the shared ``ApprovalGate``;
* creates an approval-bound ``ExecutionGrant``;
* records a metadata-only audit intent before invoking the handler; and
* requires an explicit ``ok: true`` handler result.

One :class:`MutationLifecycle` is created per approved operation.  It owns the
audit trail and the authoritative committed/verified/durability flags, so a
timeout or cancellation after installation can never be misreported as "no
file was created".
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import time
from typing import Any, Callable, Optional

from kivo.agent import mutation_audit
from kivo.agent.approval_context import (
    ApprovalContext,
    ApprovalContextError,
    MutationPreflight,
)
from kivo.agent.approval_gate import ApprovalGate, ApprovalKind
from kivo.agent.authorization import (
    AuthorizationAction,
    AuthorizationDecision,
    AuthorizationPolicy,
)
from kivo.agent.mutation_budget import MutationBudget
from kivo.agent.mutation_lifecycle import (
    MutationLifecycle,
    MutationLifecycleRegistry,
)
from kivo.agent.tool_policy import (
    MAX_EXECUTION_TIMEOUT,
    ToolPolicyError,
    validate_tool_call,
)
from kivo.agent.tool_registry import ToolRegistry
from kivo.agent.tool_schema import (
    ExecutionGrant,
    PermissionLevel,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

_MUTATION_FORBIDDEN_ARGUMENTS = frozenset(
    {
        "approval_context",
        "approval_fingerprint",
        "approval_token",
        "approved",
        "authorization",
        "authorized",
        "decision",
        "execution_grant",
        "mutation_enabled",
        "tool_call_id",
        "tool_name",
        "turn_id",
    }
)

_MUTATION_APPROVAL_STATUS = {
    "rejected": "rejected",
    "expired": "expired",
    "cancelled": "cancelled",
    "too_many_pending": "too_many_pending",
    "timeout": "timed_out",
    "timed_out": "timed_out",
    "denied": "denied",
    "gate_unavailable": "approval_error",
    "gate_error": "approval_error",
    "invalid_response": "approval_error",
    "invalid_approval_request": "approval_error",
    "synchronous_handler": "approval_error",
}


class ToolExecutor:
    """Executes validated tool calls safely within timeout boundaries."""

    def __init__(
        self,
        registry: ToolRegistry,
        approval_gate: Optional[ApprovalGate] = None,
        authorization_policy: Optional[AuthorizationPolicy] = None,
        mutation_budget: Optional[MutationBudget] = None,
        mutation_lifecycle_registry: Optional[MutationLifecycleRegistry] = None,
        on_late_mutation_result: Optional[Callable[[ToolResult], None]] = None,
    ):
        self._registry = registry
        self._approval_gate = approval_gate
        self._authorization_policy = authorization_policy or AuthorizationPolicy()
        self._mutation_budget = mutation_budget or MutationBudget()
        self._mutation_lifecycles = (
            mutation_lifecycle_registry or MutationLifecycleRegistry()
        )
        self._on_late_mutation_result = on_late_mutation_result

    @property
    def mutation_lifecycles(self) -> MutationLifecycleRegistry:
        """Expose the authoritative mutation ledger for cancellation drains."""
        return self._mutation_lifecycles

    def drain_unreported_mutation_results(self, turn_id: str = "") -> list[ToolResult]:
        """Return committed mutation results that never reached the UI.

        A cancellation or shutdown can discard the handler's ``ToolResult``
        after the file has already been installed.  The ledger still knows the
        operation is committed, so these results must be delivered before the
        turn is finalized.
        """
        results: list[ToolResult] = []
        for snapshot in self._mutation_lifecycles.drain_unreported(turn_id):
            results.append(snapshot.to_tool_result())
        return results

    async def execute(
        self,
        call: ToolCall,
        executed_ids: set[str],
        turn_id: str = "",
    ) -> ToolResult:
        """Validate, authorize, and execute a tool call asynchronously."""
        tool_name = call.tool_name
        t0 = time.monotonic()

        tool_def = self._registry.get_tool(tool_name)
        if tool_def is None:
            return self._failure(
                call,
                t0,
                f"Unknown tool: '{tool_name}'",
                {"error": f"Tool '{tool_name}' is not registered or supported in this runtime."},
            )

        # A private snapshot is what gets validated, approved, and executed.
        # This closes the gap where approved arguments could be mutated before
        # the handler ran.
        try:
            arguments = (
                copy.deepcopy(call.arguments)
                if call.arguments is not None
                else {}
            )
        except Exception as exc:
            return self._failure(
                call,
                t0,
                f"Arguments for tool '{tool_name}' could not be safely copied: {exc}",
            )

        # Everything below validates, authorizes, approves, and executes this
        # private snapshot. The caller's mutable argument dict is never read
        # again, so post-validation mutation cannot change what runs.
        snapshot_call = ToolCall(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            arguments=arguments,
        )

        mutating = bool(getattr(tool_def, "mutating", False))
        approval_context: Optional[ApprovalContext] = None
        lifecycle: Optional[MutationLifecycle] = None

        # The mutation budget is claimed before any mutating-tool validation or
        # preflight. Rejected, invalid, expired, cancelled, and failed attempts
        # therefore all consume the one attempt for this turn.
        if mutating:
            if not isinstance(turn_id, str) or not turn_id.strip():
                self._mutation_event(
                    tool_name, turn_id, call.tool_call_id, "validation_failed"
                )
                return self._failure(
                    call,
                    t0,
                    (
                        f"Mutating tool '{tool_name}' is not bound to a conversation "
                        f"turn; execution refused."
                    ),
                )
            if not self._mutation_budget.claim(turn_id):
                self._mutation_event(
                    tool_name, turn_id, call.tool_call_id, "budget_exhausted"
                )
                return self._failure(
                    call,
                    t0,
                    (
                        f"The conversation turn has already used its single mutation "
                        f"attempt. Tool '{tool_name}' was not executed."
                    ),
                )
            if self._contains_forbidden_mutation_argument(arguments):
                self._mutation_event(
                    tool_name, turn_id, call.tool_call_id, "validation_failed"
                )
                return self._failure(
                    call,
                    t0,
                    (
                        f"Mutating tool '{tool_name}' received model-supplied "
                        f"authorization data; execution refused."
                    ),
                )
            if (
                tool_def.permission != PermissionLevel.CONFIRMATION_REQUIRED
                or tool_def.legacy_binding is not None
            ):
                self._mutation_event(
                    tool_name, turn_id, call.tool_call_id, "validation_failed"
                )
                return self._failure(
                    call,
                    t0,
                    (
                        f"Mutating tool '{tool_name}' must be explicitly "
                        f"confirmation-required and must not use a legacy binding."
                    ),
                )

        try:
            validate_tool_call(snapshot_call, tool_def, executed_ids)
        except ToolPolicyError as exc:
            if mutating:
                self._mutation_event(
                    tool_name, turn_id, call.tool_call_id, "validation_failed"
                )
            else:
                self._audit(
                    tool_name,
                    "*",
                    decision="deny",
                    turn_id=turn_id,
                    tool_call_id=call.tool_call_id,
                    outcome="validation_failed",
                    result=str(exc),
                )
            return self._failure(
                call,
                t0,
                str(exc),
                {"error": f"Security policy blocked tool '{tool_name}': {exc}"},
            )

        try:
            decision = self._authorization_policy.decide(
                snapshot_call,
                tool_def,
                turn_id=turn_id,
            )
        except Exception as exc:
            if mutating:
                self._mutation_event(
                    tool_name, turn_id, call.tool_call_id, "validation_failed"
                )
            return self._failure(
                call,
                t0,
                f"Authorization policy failed closed for tool '{tool_name}': {exc}",
            )

        if decision.action == AuthorizationAction.DENY:
            if mutating:
                self._mutation_event(
                    tool_name, turn_id, call.tool_call_id, "denied"
                )
            else:
                self._audit(
                    tool_name,
                    decision.action_verb or "*",
                    decision=decision.action.value,
                    risk=decision.risk,
                    control=decision.control,
                    turn_id=turn_id,
                    tool_call_id=call.tool_call_id,
                    outcome="denied",
                    result=decision.reason,
                )
            return self._failure(
                call,
                t0,
                decision.reason or f"Tool '{tool_name}' is denied by policy.",
                {"error": decision.reason or f"Tool '{tool_name}' is denied by policy."},
            )

        # Defence in depth: authorization must never turn a mutation into an
        # automatic ALLOW. This check remains even if a policy is replaced.
        if mutating and decision.action != AuthorizationAction.REQUIRE_APPROVAL:
            self._mutation_event(
                tool_name, turn_id, call.tool_call_id, "policy_rejected"
            )
            return self._failure(
                call,
                t0,
                (
                    f"Mutating tool '{tool_name}' was not classified as requiring "
                    f"explicit approval; execution refused."
                ),
            )

        # Preflight validation runs before any approval card is shown. Its
        # result is retained: a mutating tool must return the binding that the
        # executor will turn into a trusted ApprovalContext.
        preflight = getattr(tool_def, "preflight", None)
        preflight_result: Any = None
        if callable(preflight):
            try:
                preflight_result = preflight(arguments)
                if inspect.isawaitable(preflight_result):
                    preflight_result = await preflight_result
            except Exception as exc:
                if mutating:
                    self._mutation_event(
                        tool_name, turn_id, call.tool_call_id, "preflight_failed"
                    )
                else:
                    self._audit(
                        tool_name,
                        decision.action_verb or "*",
                        decision=AuthorizationAction.DENY.value,
                        risk=decision.risk,
                        control=decision.control,
                        turn_id=turn_id,
                        tool_call_id=call.tool_call_id,
                        outcome="validation_failed",
                        result=str(exc),
                    )
                return self._failure(
                    call,
                    t0,
                    str(exc),
                    {"error": f"Tool '{tool_name}' validation failed: {exc}"},
                )

        if mutating:
            try:
                preflight_value = MutationPreflight.from_value(preflight_result)
                approval_context = preflight_value.bind(
                    tool_name=tool_def.name,
                    turn_id=turn_id,
                    tool_call_id=call.tool_call_id,
                )
            except ApprovalContextError as exc:
                self._mutation_event(
                    tool_name, turn_id, call.tool_call_id, "preflight_failed"
                )
                return self._failure(
                    call,
                    t0,
                    (
                        f"Mutating tool '{tool_name}' did not produce a valid "
                        f"approval context: {exc}"
                    ),
                )

        if decision.action == AuthorizationAction.REQUIRE_APPROVAL:
            # An approval must belong to exactly one conversation turn. A
            # missing turn cannot be approved safely, because there would be
            # no stable identifier to bind the human decision to.
            if not isinstance(turn_id, str) or not turn_id.strip():
                self._audit(
                    tool_name,
                    decision.action_verb or "*",
                    decision=AuthorizationAction.DENY.value,
                    risk=decision.risk,
                    control=decision.control,
                    turn_id="",
                    tool_call_id=call.tool_call_id,
                    outcome="denied",
                    result="approval requested without a conversation turn",
                )
                return self._failure(
                    call,
                    t0,
                    (
                        f"Tool '{tool_name}' requires approval, but the call is "
                        f"not bound to a conversation turn; execution refused."
                    ),
                )
            if mutating and approval_context is not None:
                lifecycle = self._mutation_lifecycles.create(approval_context)
                if self._on_late_mutation_result is not None:
                    observer = self._on_late_mutation_result
                    lifecycle.set_late_observer(
                        lambda snap, obs=observer: obs(snap.to_tool_result())
                    )
            try:
                approval_error, grant, approval_status = await self._request_approval(
                    snapshot_call,
                    tool_def,
                    decision,
                    arguments,
                    turn_id,
                    approval_context,
                )
            except asyncio.CancelledError:
                if lifecycle is not None:
                    lifecycle.mark_approval_failed("cancelled")
                raise
            if approval_error is not None:
                if lifecycle is not None:
                    lifecycle.mark_approval_failed(
                        _MUTATION_APPROVAL_STATUS.get(
                            approval_status, "approval_error"
                        )
                    )
                return self._failure(call, t0, approval_error)
        else:
            grant = self._grant(snapshot_call, tool_def, decision, turn_id)

        # A mutation must never run without a successful audit intent.  The
        # lifecycle owns the approval and execution-start events, so exactly
        # one of each is recorded for this operation.
        if mutating:
            if approval_context is None or lifecycle is None:
                self._mutation_event(
                    tool_name, turn_id, call.tool_call_id, "validation_failed"
                )
                return self._failure(
                    call,
                    t0,
                    (
                        f"Mutating tool '{tool_name}' did not establish a trusted "
                        f"approval context; execution refused."
                    ),
                )
            lifecycle.mark_approved()
            if not lifecycle.start():
                return self._failure(
                    call,
                    t0,
                    (
                        f"Mutation audit intent for tool '{tool_name}' could not be "
                        f"recorded; execution refused."
                    ),
                    {
                        "error": (
                            f"Mutation audit intent for tool '{tool_name}' could "
                            f"not be recorded; execution refused."
                        ),
                        "status": "audit_unavailable",
                        "committed": False,
                    },
                )

        executed_ids.add(call.tool_call_id)

        timeout = min(float(tool_def.timeout_seconds), MAX_EXECUTION_TIMEOUT)
        try:
            output = await self._invoke_handler(
                tool_def,
                arguments,
                call.tool_call_id,
                grant,
                timeout,
                turn_id,
                approval_context,
                lifecycle,
            )
            execution_time = time.monotonic() - t0

            if mutating and lifecycle is not None:
                lifecycle.complete(output)

            failure_message = self._handler_failure_message(
                output,
                tool_name=tool_name,
                mutating=mutating,
            )
            if failure_message is not None:
                if mutating:
                    if lifecycle is not None and lifecycle.committed:
                        lifecycle.mark_reported()
                    safe_output = (
                        dict(output) if isinstance(output, dict) else output
                    )
                    if (
                        lifecycle is not None
                        and lifecycle.audit_warning
                        and isinstance(safe_output, dict)
                    ):
                        safe_output.setdefault(
                            "audit_warning",
                            "The mutation failure could not be fully audited.",
                        )
                    output = safe_output
                else:
                    self._audit(
                        tool_name,
                        decision.action_verb or "*",
                        decision=decision.action.value,
                        risk=decision.risk,
                        control=decision.control,
                        turn_id=turn_id,
                        tool_call_id=call.tool_call_id,
                        outcome="failed",
                        result=failure_message,
                    )
                return ToolResult(
                    tool_call_id=call.tool_call_id,
                    tool_name=tool_name,
                    output=output,
                    success=False,
                    execution_time=round(execution_time, 3),
                    error_message=failure_message,
                    max_message_chars=tool_def.max_output_chars,
                )

            if mutating:
                if lifecycle is not None:
                    if lifecycle.committed:
                        lifecycle.mark_reported()
                    if lifecycle.audit_warning:
                        safe_output = (
                            dict(output) if isinstance(output, dict) else output
                        )
                        if isinstance(safe_output, dict):
                            safe_output.setdefault(
                                "audit_warning",
                                "Mutation completed, but its audit trail is "
                                "incomplete.",
                            )
                        output = safe_output
            else:
                self._audit(
                    tool_name,
                    decision.action_verb or "*",
                    decision=decision.action.value,
                    risk=decision.risk,
                    control=decision.control,
                    turn_id=turn_id,
                    tool_call_id=call.tool_call_id,
                    outcome="executed",
                )
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=tool_name,
                output=output,
                success=True,
                execution_time=round(execution_time, 3),
                max_message_chars=tool_def.max_output_chars,
            )

        except asyncio.TimeoutError:
            execution_time = time.monotonic() - t0
            if mutating and lifecycle is not None:
                lifecycle.record_timeout(committed=lifecycle.committed)
                if lifecycle.committed:
                    # Installation already happened.  Report the authoritative
                    # lifecycle outcome instead of falsely claiming no file was
                    # created.
                    snapshot = lifecycle.snapshot()
                    lifecycle.mark_reported()
                    error_message = "" if snapshot.ok else (
                        snapshot.error
                        or "The file was committed but verification or "
                        "durability did not complete before the timeout."
                    )
                    return ToolResult(
                        tool_call_id=call.tool_call_id,
                        tool_name=tool_name,
                        output=snapshot.to_output(),
                        success=snapshot.ok,
                        execution_time=round(execution_time, 3),
                        error_message=error_message,
                        max_message_chars=tool_def.max_output_chars,
                    )
                if lifecycle.settlement_pending:
                    # The handler returned while the background operation was
                    # still running. Abandon settlement to arm late observer delivery
                    # if it completes later.
                    already_committed = lifecycle.abandon_settlement()
                    if already_committed or lifecycle.committed:
                        snapshot = lifecycle.snapshot()
                        lifecycle.mark_reported()
                        return ToolResult(
                            tool_call_id=call.tool_call_id,
                            tool_name=tool_name,
                            output=snapshot.to_output(),
                            success=snapshot.ok,
                            execution_time=round(execution_time, 3),
                            error_message="" if snapshot.ok else (
                                snapshot.error or "Committed during timeout settlement."
                            ),
                            max_message_chars=tool_def.max_output_chars,
                        )
                    return ToolResult(
                        tool_call_id=call.tool_call_id,
                        tool_name=tool_name,
                        output={
                            "error": (
                                f"Tool '{tool_name}' execution timed out after "
                                f"{timeout}s while the filesystem operation was still running; "
                                f"its final installation state is unknown."
                            ),
                            "status": "outcome_unknown",
                            "committed": False,
                            "outcome_unknown": True,
                        },
                        success=False,
                        execution_time=round(execution_time, 3),
                        error_message=(
                            f"Execution timed out after {timeout}s while the "
                            f"filesystem operation was still running; outcome is unknown."
                        ),
                        max_message_chars=tool_def.max_output_chars,
                    )
                return ToolResult(
                    tool_call_id=call.tool_call_id,
                    tool_name=tool_name,
                    output={
                        "error": (
                            f"Tool '{tool_name}' execution timed out after "
                            f"{timeout}s before the file was installed."
                        ),
                        "status": "timed_out",
                        "committed": False,
                    },
                    success=False,
                    execution_time=round(execution_time, 3),
                    error_message=(
                        f"Execution timed out after {timeout}s before the file "
                        f"was installed."
                    ),
                    max_message_chars=tool_def.max_output_chars,
                )
            self._audit(
                tool_name,
                decision.action_verb or "*",
                decision=decision.action.value,
                risk=decision.risk,
                control=decision.control,
                turn_id=turn_id,
                tool_call_id=call.tool_call_id,
                outcome="timeout",
                result="Execution timed out",
            )
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=tool_name,
                output={"error": f"Tool '{tool_name}' execution timed out after {timeout}s."},
                success=False,
                execution_time=round(execution_time, 3),
                error_message=f"Execution timed out after {timeout}s.",
                max_message_chars=tool_def.max_output_chars,
            )
        except asyncio.CancelledError:
            if mutating and lifecycle is not None:
                # The committed flag is authoritative.  A committed result is
                # kept unreported and drained by the conversation manager once
                # cancellation settles; it is never described as "not created".
                lifecycle.record_cancelled(committed=lifecycle.committed)
            raise
        except Exception as exc:
            execution_time = time.monotonic() - t0
            if mutating and lifecycle is not None:
                lifecycle.record_failure(
                    "execution_error",
                    str(exc),
                    committed=lifecycle.committed,
                )
            else:
                self._audit(
                    tool_name,
                    decision.action_verb or "*",
                    decision=decision.action.value,
                    risk=decision.risk,
                    control=decision.control,
                    turn_id=turn_id,
                    tool_call_id=call.tool_call_id,
                    outcome="error",
                    result=str(exc),
                )
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=tool_name,
                output={"error": f"Tool '{tool_name}' raised an error: {exc}"},
                success=False,
                execution_time=round(execution_time, 3),
                error_message=str(exc),
                max_message_chars=tool_def.max_output_chars,
            )

    # ── Approval ───────────────────────────────────────────────────────────

    async def _request_approval(
        self,
        call: ToolCall,
        tool_def: ToolDefinition,
        decision: AuthorizationDecision,
        arguments: dict[str, Any],
        turn_id: str,
        approval_context: Optional[ApprovalContext] = None,
    ) -> tuple[Optional[str], Optional[ExecutionGrant], str]:
        """Request human approval for exactly this turn/tool call.

        Returns ``(error, grant, status)``.  A missing/erroring gate, malformed
        response, rejection, expiration, or cancellation always fails closed.
        """
        if not inspect.iscoroutinefunction(tool_def.handler):
            return (
                f"Tool '{call.tool_name}' is a synchronous mutating handler and "
                f"cannot safely use the approval gate; execution refused.",
                None,
                "synchronous_handler",
            )

        if self._approval_gate is None:
            return (
                f"Tool '{call.tool_name}' requires approval, but no approval gate "
                f"is available; execution refused.",
                None,
                "gate_unavailable",
            )

        mutating = approval_context is not None
        gate_kwargs: dict[str, Any] = {}
        if mutating:
            command = approval_context.operation_kind
            argument_list: list[str] = []
            working_directory = ""
            reason = (
                approval_context.display.summary
                or approval_context.display.title
                or f"Approve mutation '{call.tool_name}'."
            )
            gate_kwargs = {
                "approval_kind": ApprovalKind.FILESYSTEM_MUTATION,
                "details": approval_context.to_approval_details(),
                "require_tool_call_id": True,
                "timeout_seconds": tool_def.approval_timeout_seconds,
            }
        else:
            command, argument_list, working_directory, reason = self._approval_details(
                call,
                tool_def,
                arguments,
            )

        if not mutating:
            self._audit(
                call.tool_name,
                decision.action_verb or "*",
                decision=decision.action.value,
                risk=decision.risk,
                control=decision.control,
                turn_id=turn_id,
                tool_call_id=call.tool_call_id,
                outcome="approval_requested",
                result=reason,
            )

        try:
            response = await self._approval_gate.request_approval(
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                command=command,
                arguments=argument_list,
                working_directory=working_directory,
                reason=reason,
                turn_id=turn_id,
                **gate_kwargs,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if not mutating:
                self._audit(
                    call.tool_name,
                    decision.action_verb or "*",
                    decision=decision.action.value,
                    risk=decision.risk,
                    control=decision.control,
                    turn_id=turn_id,
                    tool_call_id=call.tool_call_id,
                    outcome="approval_error",
                    result="approval gate failed",
                )
            return (
                f"Approval gate failed for tool '{call.tool_name}'; execution refused.",
                None,
                "gate_error",
            )

        valid_response = (
            isinstance(response, (tuple, list))
            and len(response) == 2
            and isinstance(response[0], bool)
            and isinstance(response[1], str)
        )
        if not valid_response:
            if not mutating:
                self._audit(
                    call.tool_name,
                    decision.action_verb or "*",
                    decision=decision.action.value,
                    risk=decision.risk,
                    control=decision.control,
                    turn_id=turn_id,
                    tool_call_id=call.tool_call_id,
                    outcome="approval_error",
                    result="malformed approval response",
                )
            return (
                f"Approval gate returned an invalid response for tool "
                f"'{call.tool_name}'; execution refused.",
                None,
                "invalid_response",
            )

        approved, status = response
        if not isinstance(status, str):
            status = "invalid"
        if mutating and approved and status != "approved":
            approved = False

        if not mutating:
            self._audit(
                call.tool_name,
                decision.action_verb or "*",
                decision=decision.action.value,
                risk=decision.risk,
                control=decision.control,
                turn_id=turn_id,
                tool_call_id=call.tool_call_id,
                outcome=f"approval_{status}",
                result=status,
            )

        if approved:
            return None, self._grant(
                call, tool_def, decision, turn_id, approval_context
            ), "approved"

        if status == "rejected":
            message = f"The user rejected approval for tool '{call.tool_name}'."
        elif status == "expired":
            message = f"Approval request for tool '{call.tool_name}' expired."
        elif status == "cancelled":
            message = f"Approval request for tool '{call.tool_name}' was cancelled."
        elif status == "too_many_pending":
            message = (
                f"Approval request for tool '{call.tool_name}' was refused because "
                f"too many approvals are already pending."
            )
        else:
            message = f"Tool '{call.tool_name}' was not approved ({status})."
        return message, None, status

    def _approval_details(
        self,
        call: ToolCall,
        tool_def: ToolDefinition,
        arguments: dict[str, Any],
    ) -> tuple[str, list[str], str, str]:
        """Build the human-facing approval summary without leaking arbitrary args."""
        if isinstance(arguments.get("command"), str):
            command = arguments["command"].strip()
            raw_arguments = arguments.get("arguments", [])
            argument_list = (
                [str(item) for item in raw_arguments]
                if isinstance(raw_arguments, list)
                else []
            )
            working_directory = (
                arguments.get("working_directory")
                if isinstance(arguments.get("working_directory"), str)
                else ""
            )
            reason = (
                arguments.get("reason")
                if isinstance(arguments.get("reason"), str)
                else f"Approve execution of '{command}'."
            )
            return command, argument_list, working_directory, reason

        # Generic mutating tools do not expose their raw argument values to the
        # approval UI; the tool name is enough to decide yes or no.
        return (
            call.tool_name,
            [],
            "",
            f"Approve execution of the '{call.tool_name}' tool.",
        )

    # ── Invocation ─────────────────────────────────────────────────────────

    async def _invoke_handler(
        self,
        tool_def: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str,
        grant: ExecutionGrant,
        timeout: float,
        turn_id: str = "",
        approval_context: Optional[ApprovalContext] = None,
        lifecycle: Optional[MutationLifecycle] = None,
    ) -> Any:
        extra_kwargs: dict[str, Any] = {}
        try:
            signature = inspect.signature(tool_def.handler)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            if "tool_call_id" in signature.parameters:
                extra_kwargs["tool_call_id"] = tool_call_id
            if "execution_grant" in signature.parameters:
                extra_kwargs["execution_grant"] = grant
            if "turn_id" in signature.parameters:
                extra_kwargs["turn_id"] = turn_id
            if "approval_context" in signature.parameters:
                extra_kwargs["approval_context"] = approval_context
            if "mutation_lifecycle" in signature.parameters:
                extra_kwargs["mutation_lifecycle"] = lifecycle
            if "defer_lifecycle_settlement" in signature.parameters:
                extra_kwargs["defer_lifecycle_settlement"] = (
                    bool(getattr(tool_def, "mutating", False))
                    and lifecycle is not None
                )

        if inspect.iscoroutinefunction(tool_def.handler):
            return await asyncio.wait_for(
                tool_def.handler(arguments, **extra_kwargs),
                timeout=timeout,
            )

        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: tool_def.handler(arguments, **extra_kwargs),
            ),
            timeout=timeout,
        )

    def _grant(
        self,
        call: ToolCall,
        tool_def: ToolDefinition,
        decision: AuthorizationDecision,
        turn_id: str,
        approval_context: Optional[ApprovalContext] = None,
    ) -> ExecutionGrant:
        if getattr(tool_def, "mutating", False):
            if (
                approval_context is None
                or decision.action != AuthorizationAction.REQUIRE_APPROVAL
            ):
                raise ValueError(
                    "A mutation grant requires an approved approval context."
                )
            return ExecutionGrant(
                turn_id=turn_id,
                tool_call_id=call.tool_call_id,
                tool_name=tool_def.name,
                decision=AuthorizationAction.REQUIRE_APPROVAL.value,
                authorized=True,
                approved=True,
                approval_fingerprint=approval_context.fingerprint,
            )
        return ExecutionGrant(
            turn_id=turn_id,
            tool_call_id=call.tool_call_id,
            tool_name=tool_def.name,
            decision=decision.action.value,
            authorized=True,
        )

    # ── Result handling ────────────────────────────────────────────────────

    @classmethod
    def _handler_failure_message(
        cls,
        output: Any,
        *,
        tool_name: str,
        mutating: bool,
    ) -> Optional[str]:
        """Return a failure message for an explicit failure result.

        Read-only tools retain their historical behavior when ``ok`` is absent.
        Mutating tools must return ``ok is True`` explicitly.
        """
        if not isinstance(output, dict):
            if mutating:
                return (
                    f"Mutating tool '{tool_name}' did not return an explicit "
                    f"ok: true result."
                )
            return None

        ok = output.get("ok")
        if mutating and ok is not True:
            if ok is False:
                detail = output.get("error") or output.get("message")
                if isinstance(detail, str) and detail.strip():
                    return detail.strip()[:500]
                return f"Mutating tool '{tool_name}' reported failure."
            return (
                f"Mutating tool '{tool_name}' did not return an explicit "
                f"ok: true result."
            )

        if not mutating and ok is False:
            detail = output.get("error") or output.get("message")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()[:500]
            return f"Tool '{tool_name}' reported failure."
        return None

    # ── Mutation helpers ───────────────────────────────────────────────────

    @classmethod
    def _contains_forbidden_mutation_argument(
        cls, value: Any, depth: int = 0
    ) -> bool:
        if depth > 6:
            return True
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in _MUTATION_FORBIDDEN_ARGUMENTS:
                    return True
                if cls._contains_forbidden_mutation_argument(item, depth + 1):
                    return True
            return False
        if isinstance(value, (list, tuple)):
            return any(
                cls._contains_forbidden_mutation_argument(item, depth + 1)
                for item in value
            )
        return False

    @staticmethod
    def _mutation_event(
        tool_name: str,
        turn_id: str,
        tool_call_id: str,
        status: str,
    ) -> None:
        """Metadata-only event for failures before an ApprovalContext exists."""
        try:
            mutation_audit.record_mutation_event(
                tool_name=tool_name or "unknown_tool",
                operation_kind=tool_name or "mutation",
                status=status,
                turn_id=turn_id if isinstance(turn_id, str) else "",
                tool_call_id=(
                    tool_call_id if isinstance(tool_call_id, str) else ""
                ),
            )
        except Exception:
            pass

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _failure(
        call: ToolCall,
        started_at: float,
        message: str,
        output: Optional[dict[str, Any]] = None,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            output=output or {"error": message},
            success=False,
            execution_time=round(time.monotonic() - started_at, 3),
            error_message=message,
        )

    @staticmethod
    def _audit(
        tool: str,
        action: str,
        *,
        decision: str,
        turn_id: str,
        tool_call_id: str,
        outcome: str,
        risk: Any = None,
        control: Any = None,
        result: str = "",
    ) -> None:
        try:
            from core.action_policy import Control, Risk, record_execution

            record_execution(
                tool=tool,
                action=action or "*",
                parameters={},
                risk=risk if risk is not None else Risk.SAFE,
                control=control if control is not None else Control.LOG,
                outcome=outcome,
                result=result,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                decision=decision,
            )
        except Exception:
            pass


__all__ = ["ToolExecutor"]
