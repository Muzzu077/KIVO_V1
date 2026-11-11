"""Authoritative, metadata-only lifecycle records for mutation attempts.

The executor, service, and handler share one :class:`MutationLifecycle` object
per approved operation.  The lifecycle is the single source of truth for
whether a file was committed, verified, and made durable.  This matters because
an asyncio timeout or cancellation can otherwise discard a result after
``renameat2`` has already installed the file.

The ledger is deliberately in-memory and bounded.  It stores only identifiers
and already-approved binding metadata; file contents, absolute workspace roots,
filesystem identity tokens, approval tokens, and credentials are never copied
into a lifecycle result or an audit event.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from kivo.agent import mutation_audit
from kivo.agent.approval_context import ApprovalContext

MAX_MUTATION_LIFECYCLE_RECORDS = 64

DURABILITY_UNKNOWN = "unknown"
DURABILITY_SYNCED = "synced"
DURABILITY_FAILED = "failed"

TERMINAL_COMMITTED = "committed"
TERMINAL_CANCELLED = "cancelled"
TERMINAL_TIMED_OUT = "timed_out"
TERMINAL_FAILED = "failed"


@dataclass(frozen=True)
class MutationResult:
    """Public, UI-safe snapshot of one mutation operation."""

    operation_id: str
    tool_name: str
    turn_id: str
    tool_call_id: str
    workspace_id: str
    relative_path: str
    content_digest: str
    byte_count: int
    committed: bool
    verified: bool
    durability: str
    status: str
    error: str = ""
    cancelled_after_commit: bool = False
    timed_out_after_commit: bool = False
    outcome_unknown: bool = False
    audit_warning: bool = False
    reported: bool = False

    @property
    def ok(self) -> bool:
        """True only when the file is committed and verification succeeded."""
        return (
            self.committed
            and self.verified
            and self.durability != DURABILITY_FAILED
            and not self.outcome_unknown
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a bounded metadata-only representation."""
        return {
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "workspace_id": self.workspace_id,
            "relative_path": self.relative_path,
            "content_digest": self.content_digest,
            "byte_count": self.byte_count,
            "committed": self.committed,
            "verified": self.verified,
            "durability": self.durability,
            "status": self.status,
            "error": self.error,
            "cancelled_after_commit": self.cancelled_after_commit,
            "timed_out_after_commit": self.timed_out_after_commit,
            "outcome_unknown": self.outcome_unknown,
            "audit_warning": self.audit_warning,
            "reported": self.reported,
        }

    def to_output(self) -> dict[str, Any]:
        """Return the tool-result payload, including an honest committed notice."""
        output = self.to_dict()
        output["ok"] = self.ok
        if self.committed:
            output["committed_notice"] = (
                "The file exists at the approved path. Do not report that no "
                "file was created."
            )
        if self.audit_warning:
            output["audit_warning"] = (
                "The file was created, but its mutation audit trail is "
                "incomplete; the filesystem outcome is reported from the "
                "authoritative lifecycle record."
                if self.committed
                else "The mutation audit trail is incomplete; the filesystem "
                "outcome is reported from the authoritative lifecycle record."
            )
        return output

    def to_tool_result(self):
        """Build a ToolResult for a committed operation recovered after cancel."""
        from kivo.agent.tool_schema import ToolResult

        output = self.to_output()
        error = ""
        if not self.ok:
            error = self.error or (
                "The file was committed but verification or durability did not "
                "complete successfully."
            )
        return ToolResult(
            tool_call_id=self.tool_call_id,
            tool_name=self.tool_name,
            output=output,
            success=self.ok,
            execution_time=0.0,
            error_message=error,
        )


class MutationLifecycle:
    """Thread-safe state machine and audit owner for one mutation attempt."""

    def __init__(self, context: ApprovalContext):
        if not isinstance(context, ApprovalContext):
            raise TypeError("MutationLifecycle requires an ApprovalContext.")
        self._context = context
        self._operation_id = context.operation_id
        self._lock = threading.RLock()
        self._started = False
        self._approved = False
        self._committed = False
        self._verified = False
        self._durability = DURABILITY_UNKNOWN
        self._terminal_status = ""
        self._error = ""
        self._cancelled_after_commit = False
        self._timed_out_after_commit = False
        self._audit_warning = False
        self._reported = False
        self._completed_at = 0.0
        self._settlement_pending = False
        self._settlement_abandoned = False
        self._late_observer: Optional[Callable[["MutationResult"], None]] = None
        self._events: set[str] = set()
        self._verification_recorded = False
        self._durability_recorded = False

    # ── Identity ───────────────────────────────────────────────────────────

    @property
    def context(self) -> ApprovalContext:
        return self._context

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started

    @property
    def approved(self) -> bool:
        with self._lock:
            return self._approved

    @property
    def committed(self) -> bool:
        with self._lock:
            return self._committed

    @property
    def verified(self) -> bool:
        with self._lock:
            return self._verified

    @property
    def durability(self) -> str:
        with self._lock:
            return self._durability

    @property
    def audit_warning(self) -> bool:
        with self._lock:
            return self._audit_warning

    @property
    def terminal(self) -> bool:
        with self._lock:
            return bool(self._terminal_status)

    @property
    def settlement_pending(self) -> bool:
        """True when the handler returned before the operation settled."""
        with self._lock:
            return self._settlement_pending

    @property
    def settlement_abandoned(self) -> bool:
        """True once the caller has returned an unknown, non-committed result."""
        with self._lock:
            return self._settlement_abandoned

    # ── Audit plumbing ─────────────────────────────────────────────────────

    def _audit(self, status: str, **extra: Any) -> bool:
        binding = self._context.binding
        try:
            accepted = mutation_audit.record_mutation_event(
                tool_name=self._context.tool_name,
                operation_kind=self._context.operation_kind,
                status=status,
                turn_id=self._context.turn_id,
                tool_call_id=self._context.tool_call_id,
                operation_id=self._operation_id,
                workspace_id=binding.workspace_id,
                relative_path=binding.relative_path,
                content_digest=binding.content_digest,
                byte_count=binding.byte_count,
                **extra,
            )
        except Exception:
            accepted = False
        if not accepted:
            with self._lock:
                self._audit_warning = True
        return bool(accepted)

    def _audit_once(self, event: str, status: str, **extra: Any) -> bool:
        # The lock is held across the in-memory audit append so two concurrent
        # transitions for the same event cannot both pass the membership check
        # and produce duplicate events.  The audit sink is a bounded in-memory
        # ring and never performs blocking I/O.
        with self._lock:
            if event in self._events:
                return True
            accepted = self._audit(status, **extra)
            if accepted:
                self._events.add(event)
            return accepted

    # ── Lifecycle transitions ──────────────────────────────────────────────

    def mark_approved(self) -> bool:
        with self._lock:
            self._approved = True
        return self._audit_once("approved", "approved")

    def mark_approval_failed(self, status: str) -> bool:
        """Record a rejected/expired/cancelled approval without execution."""
        mapping = {
            "rejected": "approval_rejected",
            "expired": "approval_expired",
            "cancelled": "approval_cancelled",
            "too_many_pending": "approval_error",
            "denied": "approval_error",
            "gate_unavailable": "approval_error",
            "gate_error": "approval_error",
            "invalid_response": "approval_error",
            "invalid_approval_request": "approval_error",
            "synchronous_handler": "approval_error",
        }
        audit_status = mapping.get(status, "approval_error")
        with self._lock:
            if not self._terminal_status:
                self._terminal_status = status or "approval_error"
        return self._audit_once("approval_" + audit_status, audit_status)

    def start(self) -> bool:
        """Record the mandatory pre-change intent exactly once.

        Returns ``False`` when the audit sink rejected the intent.  The caller
        must not invoke the handler in that case.
        """
        with self._lock:
            if self._started:
                return True
        accepted = self._audit_once("execution_started", "execution_started")
        if accepted:
            with self._lock:
                self._started = True
            return True
        return False

    def mark_committed(self) -> bool:
        """Mark the file as installed. Must be called immediately after rename."""
        with self._lock:
            self._committed = True
        return self._audit_once("committed", "committed", committed=True)

    def mark_verification(self, verified: bool) -> bool:
        with self._lock:
            if self._verified and not verified:
                # Verification is monotonic: a late duplicate cannot downgrade
                # a file that was already verified.
                return True
            self._verified = bool(verified)
            self._verification_recorded = True
        return self._audit_once(
            "verification",
            "verification_succeeded" if verified else "verification_failed",
            committed=True,
            verified=bool(verified),
        )

    def mark_durability(self, state: str) -> bool:
        if state not in (DURABILITY_UNKNOWN, DURABILITY_SYNCED, DURABILITY_FAILED):
            state = DURABILITY_UNKNOWN
        with self._lock:
            if self._durability == DURABILITY_FAILED and state != DURABILITY_FAILED:
                # Durability failure is terminal: a later success cannot make
                # an uncertain parent directory durable retroactively.
                return True
            self._durability = state
            self._durability_recorded = True
        return self._audit_once(
            "durability",
            f"durability_{state}",
            committed=True,
            durability=state,
        )

    def record_failure(
        self,
        code: str,
        message: str = "",
        *,
        committed: Optional[bool] = None,
    ) -> None:
        """Record a terminal failure without ever erasing a commit."""
        with self._lock:
            # A fully verified, durable success is terminal.  A late duplicate
            # failure callback must not rewrite its error or status.
            if (
                self._completed_at
                and self._committed
                and self._verified
                and self._durability != DURABILITY_FAILED
            ):
                return
            if committed is True:
                self._committed = True
            is_committed = self._committed
            self._error = str(message or "")[:500]
            if not self._terminal_status:
                self._terminal_status = str(code or "failed")[:64]
            already_described = self._verification_recorded or self._durability_recorded
        if is_committed:
            if not already_described:
                self._audit_once("post_commit_failure", "failed", committed=True)
            return
        audit_status = "cancelled" if code == "cancelled" else "failed"
        self._audit_once("failure", audit_status, committed=False)

    def record_cancelled(self, *, committed: Optional[bool] = None) -> bool:
        """Record cancellation; returns the effective committed state."""
        with self._lock:
            effective = self._committed if committed is None else bool(committed)
            if effective:
                self._committed = True
                self._cancelled_after_commit = True
                if not self._terminal_status:
                    self._terminal_status = TERMINAL_CANCELLED
            else:
                if not self._terminal_status:
                    self._terminal_status = TERMINAL_CANCELLED
        if effective:
            self._audit_once(
                "cancelled_after_commit",
                "cancelled_after_commit",
                committed=True,
            )
        else:
            self._audit_once("cancelled", "cancelled", committed=False)
        return effective

    def record_timeout(self, *, committed: Optional[bool] = None) -> bool:
        """Record timeout; returns the effective committed state."""
        with self._lock:
            effective = self._committed if committed is None else bool(committed)
            if effective:
                self._committed = True
                self._timed_out_after_commit = True
                if not self._terminal_status:
                    self._terminal_status = TERMINAL_TIMED_OUT
            else:
                if not self._terminal_status:
                    self._terminal_status = TERMINAL_TIMED_OUT
        if effective:
            self._audit_once(
                "timed_out_after_commit",
                "timed_out_after_commit",
                committed=True,
            )
        else:
            self._audit_once("timed_out", "timed_out", committed=False)
        return effective

    def mark_settlement_pending(self) -> None:
        """Remember that the handler returned while its operation was running."""
        with self._lock:
            if not self._committed:
                self._settlement_pending = True

    def abandon_settlement(self) -> bool:
        """Arm late-delivery after the caller returns an unknown outcome.

        Returns ``True`` when the file is already known committed; the caller
        must then report the committed snapshot instead of an unknown result.
        """
        with self._lock:
            if self._committed:
                return True
            self._settlement_abandoned = True
            self._settlement_pending = False
            return False

    def set_late_observer(
        self, observer: Optional[Callable[["MutationResult"], None]]
    ) -> None:
        if observer is not None and not callable(observer):
            raise TypeError("Late mutation observer must be callable.")
        with self._lock:
            self._late_observer = observer

    def record_unknown(self) -> None:
        """Record that the handler vanished without confirming the outcome."""
        with self._lock:
            if self._committed or self._terminal_status:
                return
            self._terminal_status = "outcome_unknown"
            self._error = (
                "The mutation did not confirm completion before the execution "
                "boundary; its filesystem outcome is unknown."
            )
        self._audit_once("outcome_unknown", "outcome_unknown", committed=False)

    def complete(self, result: Mapping[str, Any]) -> bool:
        """Complete from a handler/service result exactly once.

        A successful result supersedes an intermediate cancellation/timeout
        marker because the file may have been installed after the caller's
        settlement window closed.  The ``cancelled_after_commit`` and
        ``timed_out_after_commit`` flags preserve how the caller learned about
        the commit, and a late observer is notified exactly once.
        """
        if not isinstance(result, Mapping):
            self.record_failure("invalid_result", "Mutation result is not an object.")
            return False

        ok = result.get("ok") is True
        raw_committed = result.get("committed")
        committed = raw_committed if isinstance(raw_committed, bool) else ok
        raw_verified = result.get("verified")
        verified = raw_verified if isinstance(raw_verified, bool) else bool(ok)
        durability = result.get("durability")
        if durability not in (
            DURABILITY_UNKNOWN,
            DURABILITY_SYNCED,
            DURABILITY_FAILED,
        ):
            durability = DURABILITY_UNKNOWN

        if not ok:
            code = result.get("status")
            if not isinstance(code, str) or not code:
                code = "failed"
            message = result.get("error") or result.get("message") or ""
            self.record_failure(
                code,
                message if isinstance(message, str) else "",
                committed=committed,
            )
            return False

        if not committed:
            # A mutating handler must never report success without confirming
            # installation.  Treat that as a failure rather than a created file.
            self.record_failure(
                "not_committed",
                "The mutation reported success without confirming installation.",
                committed=False,
            )
            return False

        with self._lock:
            if self._completed_at:
                # Repeated success completions are idempotent and cannot
                # downgrade a verified/durable record.
                return True
            previous_terminal = self._terminal_status
            late_context = self._settlement_abandoned or previous_terminal in (
                TERMINAL_TIMED_OUT,
                TERMINAL_CANCELLED,
                "outcome_unknown",
            )
            self._committed = True
            if raw_verified is not None:
                self._verified = bool(verified)
            elif not self._verified:
                self._verified = bool(verified)
            if durability != DURABILITY_UNKNOWN or not self._durability_recorded:
                self._durability = durability
            status = result.get("status")
            self._terminal_status = (
                status if isinstance(status, str) and status else "created"
            )
            self._completed_at = time.time()
            self._settlement_pending = False
            if previous_terminal == TERMINAL_TIMED_OUT:
                self._timed_out_after_commit = True
            elif previous_terminal == TERMINAL_CANCELLED:
                self._cancelled_after_commit = True
            notify = bool(
                late_context
                and not self._reported
                and self._late_observer is not None
            )
            if notify:
                # Claim delivery before releasing the lock so a concurrent
                # drain cannot emit the same committed result.
                self._reported = True
            snapshot = self.snapshot()

        self.mark_committed()
        if raw_verified is not None:
            self.mark_verification(bool(verified))
        if durability != DURABILITY_UNKNOWN:
            self.mark_durability(durability)

        if late_context:
            if previous_terminal == TERMINAL_TIMED_OUT:
                self._audit_once(
                    "timed_out_after_commit",
                    "timed_out_after_commit",
                    committed=True,
                )
            elif previous_terminal == TERMINAL_CANCELLED:
                self._audit_once(
                    "cancelled_after_commit",
                    "cancelled_after_commit",
                    committed=True,
                )

        if notify:
            observer = self._late_observer
            try:
                if observer is not None:
                    observer(snapshot)
            except Exception:
                # Delivery failed; allow a later drain to retry.
                self.mark_unreported()
        return True

    # ── Snapshot / reporting ───────────────────────────────────────────────

    def snapshot(self) -> MutationResult:
        binding = self._context.binding
        with self._lock:
            terminal_status = self._terminal_status
            if not terminal_status:
                if self._committed:
                    terminal_status = "committed"
                else:
                    terminal_status = "pending"
            outcome_unknown = (
                not self._committed
                and (
                    self._settlement_pending
                    or self._settlement_abandoned
                    or not self._terminal_status
                    or terminal_status == "outcome_unknown"
                )
            )
            return MutationResult(
                operation_id=self._operation_id,
                tool_name=self._context.tool_name,
                turn_id=self._context.turn_id,
                tool_call_id=self._context.tool_call_id,
                workspace_id=binding.workspace_id,
                relative_path=binding.relative_path,
                content_digest=binding.content_digest,
                byte_count=binding.byte_count,
                committed=self._committed,
                verified=self._verified,
                durability=self._durability,
                status=terminal_status,
                error=self._error,
                cancelled_after_commit=self._cancelled_after_commit,
                timed_out_after_commit=self._timed_out_after_commit,
                outcome_unknown=outcome_unknown,
                audit_warning=self._audit_warning,
                reported=self._reported,
            )

    def mark_reported(self) -> bool:
        with self._lock:
            already = self._reported
            self._reported = True
            return not already

    def mark_unreported(self) -> bool:
        """Return a claimed result to the drainable set after delivery failure."""
        with self._lock:
            was_reported = self._reported
            self._reported = False
            return was_reported


class MutationLifecycleRegistry:
    """Bounded registry of lifecycle records for one executor."""

    def __init__(self, max_records: int = MAX_MUTATION_LIFECYCLE_RECORDS):
        self._lock = threading.RLock()
        self._max_records = max(1, int(max_records))
        self._records: dict[str, MutationLifecycle] = {}
        self._order: deque[str] = deque()

    def create(self, context: ApprovalContext) -> MutationLifecycle:
        lifecycle = MutationLifecycle(context)
        with self._lock:
            existing = self._records.get(lifecycle.operation_id)
            if existing is not None:
                return existing
            self._records[lifecycle.operation_id] = lifecycle
            self._order.append(lifecycle.operation_id)
            self._evict_locked()
        return lifecycle

    def get(self, operation_id: str) -> Optional[MutationLifecycle]:
        with self._lock:
            return self._records.get(operation_id)

    def release_reported(self, operation_id: str) -> bool:
        """Undo a delivery claim when the UI callback failed.

        The next drain can then retry the committed result instead of losing it.
        """
        lifecycle = self.get(operation_id)
        if lifecycle is None:
            return False
        return lifecycle.mark_unreported()

    def drain_unreported(self, turn_id: str = "") -> list[MutationResult]:
        """Return committed results not yet delivered to the conversation UI.

        Only committed operations are returned.  A rejected or pre-install
        cancelled operation has no file to report, and a not-yet-committed
        operation must not be described as either created or not created.
        """
        drained: list[MutationResult] = []
        with self._lock:
            records = list(self._records.values())
        for lifecycle in records:
            snapshot = lifecycle.snapshot()
            if turn_id and snapshot.turn_id != turn_id:
                continue
            if not snapshot.committed or snapshot.reported:
                continue
            if not lifecycle.mark_reported():
                continue
            # Re-snapshot so the emitted result carries the final reported
            # flag and any state written while the lock was released.
            drained.append(lifecycle.snapshot())
        return drained

    def _evict_locked(self) -> None:
        while len(self._records) > self._max_records and self._order:
            operation_id = self._order[0]
            lifecycle = self._records.get(operation_id)
            if lifecycle is None:
                self._order.popleft()
                continue
            snapshot = lifecycle.snapshot()
            if snapshot.committed and not snapshot.reported:
                # Never evict an unreported committed outcome.
                self._order.rotate(-1)
                if self._order[0] == operation_id:
                    break
                continue
            self._order.popleft()
            self._records.pop(operation_id, None)


__all__ = [
    "DURABILITY_FAILED",
    "DURABILITY_SYNCED",
    "DURABILITY_UNKNOWN",
    "MAX_MUTATION_LIFECYCLE_RECORDS",
    "MutationLifecycle",
    "MutationLifecycleRegistry",
    "MutationResult",
]
