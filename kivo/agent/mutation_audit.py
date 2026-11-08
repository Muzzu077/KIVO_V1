"""Metadata-only audit contract for future filesystem mutations.

Only the following metadata may be recorded:

* turn ID
* tool-call ID
* tool name
* workspace ID
* canonical relative path
* content digest
* byte count
* operation status

File contents, absolute workspace roots, approval tokens, and secrets are
never accepted by this module.  The audit sink is the existing in-memory
``core.action_policy`` ring; no second logging stack is introduced.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from kivo.agent.approval_context import (
    MAX_BYTE_COUNT,
    ApprovalContext,
    ApprovalContextError,
    canonicalize_relative_path,
)

MUTATION_AUDIT_STATUSES = frozenset(
    {
        "intent",
        "executed",
        "failed",
        "invalid_result",
        "rejected",
        "expired",
        "cancelled",
        "timed_out",
        "timeout",
        "denied",
        "policy_rejected",
        "budget_exhausted",
        "preflight_failed",
        "validation_failed",
        "approval_error",
        "too_many_pending",
    }
)

_ALLOWED_DETAIL_KEYS = frozenset(
    {
        "workspace_id",
        "relative_path",
        "content_digest",
        "byte_count",
    }
)

_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _validate_status(status: str) -> str:
    if not isinstance(status, str):
        raise ValueError("Mutation audit status must be a string.")
    clean = status.strip().lower()
    if clean not in MUTATION_AUDIT_STATUSES:
        raise ValueError(f"Unrecognized mutation audit status: {status!r}.")
    return clean


def _metadata(
    *,
    workspace_id: str = "",
    relative_path: str = "",
    content_digest: str = "",
    byte_count: Optional[int] = None,
) -> dict[str, Any]:
    """Build a strict, allowlisted metadata dictionary.

    Values are validated cheaply here as a second line of defence.  Callers
    normally pass values already canonicalized by ``ApprovalContext``.
    """
    parameters: dict[str, Any] = {}
    if workspace_id:
        if not isinstance(workspace_id, str) or not _WORKSPACE_ID.fullmatch(
            workspace_id
        ):
            raise ValueError("Invalid workspace identifier for audit.")
        parameters["workspace_id"] = workspace_id
    if relative_path:
        if not isinstance(relative_path, str):
            raise ValueError("Invalid relative path for audit.")
        try:
            parameters["relative_path"] = canonicalize_relative_path(relative_path)
        except ApprovalContextError as exc:
            raise ValueError("Invalid relative path for audit.") from exc
    if content_digest:
        if not isinstance(content_digest, str) or not _SHA256_HEX.fullmatch(
            content_digest
        ):
            raise ValueError("Invalid content digest for audit.")
        parameters["content_digest"] = content_digest
    if byte_count is not None:
        if (
            not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 0
            or byte_count > MAX_BYTE_COUNT
        ):
            raise ValueError("Invalid byte count for audit.")
        parameters["byte_count"] = byte_count
    if not set(parameters).issubset(_ALLOWED_DETAIL_KEYS):
        raise ValueError("Mutation audit metadata contains disallowed keys.")
    return parameters


def _record(
    *,
    tool_name: str,
    operation_kind: str,
    status: str,
    turn_id: str,
    tool_call_id: str,
    parameters: dict[str, Any],
    duration: float = 0.0,
) -> bool:
    clean_status = _validate_status(status)
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("Mutation audit tool name is required.")
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("Mutation audit turn ID is required.")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise ValueError("Mutation audit tool-call ID is required.")
    clean_operation = operation_kind.strip().lower() if isinstance(operation_kind, str) else ""
    if not clean_operation:
        clean_operation = "mutation"

    # Imported lazily so tests and embedders can replace the central sink.
    try:
        from core.action_policy import Control, Risk, record_execution

        record_execution(
            tool=tool_name,
            action=clean_operation,
            parameters=parameters,
            risk=Risk.HIGH,
            control=Control.CONFIRM,
            outcome=clean_status,
            result=clean_status,
            duration=max(0.0, float(duration)),
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            decision="require_approval",
        )
        return True
    except Exception:
        return False


def record_mutation_event(
    *,
    tool_name: str,
    operation_kind: str,
    status: str,
    turn_id: str,
    tool_call_id: str,
    workspace_id: str = "",
    relative_path: str = "",
    content_digest: str = "",
    byte_count: Optional[int] = None,
    duration: float = 0.0,
) -> bool:
    """Record one metadata-only mutation lifecycle event.

    Returns ``True`` only when the audit sink accepted the event.
    """
    try:
        parameters = _metadata(
            workspace_id=workspace_id,
            relative_path=relative_path,
            content_digest=content_digest,
            byte_count=byte_count,
        )
        return _record(
            tool_name=tool_name,
            operation_kind=operation_kind,
            status=status,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            parameters=parameters,
            duration=duration,
        )
    except Exception:
        return False


def record_mutation_intent(
    context: ApprovalContext,
    *,
    duration: float = 0.0,
) -> bool:
    """Record the required pre-execution intent for an approved mutation."""
    if not isinstance(context, ApprovalContext):
        return False
    return record_mutation_event(
        tool_name=context.tool_name,
        operation_kind=context.operation_kind,
        status="intent",
        turn_id=context.turn_id,
        tool_call_id=context.tool_call_id,
        workspace_id=context.binding.workspace_id,
        relative_path=context.binding.relative_path,
        content_digest=context.binding.content_digest,
        byte_count=context.binding.byte_count,
        duration=duration,
    )


def record_mutation_outcome(
    context: ApprovalContext,
    status: str,
    *,
    duration: float = 0.0,
) -> bool:
    """Record the final status for an approved mutation attempt."""
    if not isinstance(context, ApprovalContext):
        return False
    return record_mutation_event(
        tool_name=context.tool_name,
        operation_kind=context.operation_kind,
        status=status,
        turn_id=context.turn_id,
        tool_call_id=context.tool_call_id,
        workspace_id=context.binding.workspace_id,
        relative_path=context.binding.relative_path,
        content_digest=context.binding.content_digest,
        byte_count=context.binding.byte_count,
        duration=duration,
    )


__all__ = [
    "MUTATION_AUDIT_STATUSES",
    "record_mutation_event",
    "record_mutation_intent",
    "record_mutation_outcome",
]
