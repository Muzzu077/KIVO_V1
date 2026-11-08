"""Asynchronous human-in-the-loop approval gate for sensitive agent actions.

The gate is deliberately small but race-aware:

* every token is single-use and consumed before its waiter is woken;
* resolution may arrive from any thread, including the IPC thread;
* expiration, cancellation and user resolution are serialized under one lock;
* pending requests and the consumed-token ledger are bounded;
* tokens can be bound to the conversation turn and tool call that created them.
"""
from __future__ import annotations

import asyncio
import collections
import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 60.0
MAX_APPROVAL_TIMEOUT_SECONDS = 3_600.0
MAX_PENDING_APPROVALS = 8
MAX_CONSUMED_TOKENS = 256
MAX_APPROVAL_DETAILS = 16
MAX_APPROVAL_DETAIL_KEY_CHARS = 48
MAX_APPROVAL_DETAIL_STRING_CHARS = 512
MAX_APPROVAL_DETAILS_BYTES = 4_096
MAX_APPROVAL_KIND_CHARS = 64

_APPROVAL_DETAIL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_APPROVAL_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTROL_CHARS = frozenset(chr(code) for code in range(32)) | {chr(127)}


class ApprovalKind(str, Enum):
    """Known approval categories. The empty value is legacy-compatible."""

    UNSPECIFIED = ""
    TERMINAL_COMMAND = "terminal_command"
    FILESYSTEM_MUTATION = "filesystem_mutation"


def _clean_detail_value(value: Any, *, key: str) -> Any:
    """Return an immutable, bounded scalar suitable for IPC display."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < -((1 << 63) - 1) or value > (1 << 63) - 1:
            raise ValueError(f"Approval detail '{key}' is out of range.")
        return value
    if isinstance(value, str):
        clean = value.strip()
        if len(clean) > MAX_APPROVAL_DETAIL_STRING_CHARS:
            raise ValueError(f"Approval detail '{key}' is too long.")
        if any(ch in _CONTROL_CHARS for ch in clean):
            raise ValueError(f"Approval detail '{key}' contains control characters.")
        return clean
    raise ValueError(
        f"Approval detail '{key}' must be a string, integer, boolean, or null."
    )


def normalize_approval_details(
    details: Optional[Mapping[str, Any]],
) -> tuple[tuple[str, Any], ...]:
    """Validate and freeze bounded approval details.

    Details are a flat mapping of short lowercase keys to scalar values. Nested
    structures, bytes, floats, and oversized values are rejected before a
    request can become pending.  The returned tuple is deterministic (sorted by
    key) and safe to copy into an approval request.
    """
    if details is None:
        return ()
    if not isinstance(details, Mapping):
        raise ValueError("Approval details must be a mapping.")
    if len(details) > MAX_APPROVAL_DETAILS:
        raise ValueError("Too many approval detail entries.")
    frozen: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for key, value in details.items():
        if not isinstance(key, str) or not _APPROVAL_DETAIL_KEY.fullmatch(key):
            raise ValueError("Approval detail keys must be short lowercase tokens.")
        if key in seen:
            raise ValueError(f"Duplicate approval detail key: '{key}'.")
        seen.add(key)
        frozen.append((key, _clean_detail_value(value, key=key)))
    frozen.sort(key=lambda item: item[0])
    serialized = json.dumps(
        dict(frozen),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    if len(serialized.encode("utf-8")) > MAX_APPROVAL_DETAILS_BYTES:
        raise ValueError("Approval details exceed the maximum serialized size.")
    return tuple(frozen)


def normalize_approval_kind(kind: Any) -> str:
    """Validate an approval kind while keeping the legacy empty value."""
    if isinstance(kind, ApprovalKind):
        return kind.value
    if not isinstance(kind, str):
        raise ValueError("Approval kind must be a string.")
    clean = kind.strip().lower()
    if not clean:
        return ""
    if len(clean) > MAX_APPROVAL_KIND_CHARS or not _APPROVAL_KIND.fullmatch(clean):
        raise ValueError("Approval kind must be a short lowercase token.")
    return clean


def normalize_request_timeout(timeout_seconds: Any) -> Optional[float]:
    """Validate an optional per-request timeout."""
    if timeout_seconds is None:
        return None
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds, (int, float)
    ):
        raise ValueError("Approval timeout must be a finite number.")
    value = float(timeout_seconds)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("Approval timeout must be greater than zero.")
    return min(value, MAX_APPROVAL_TIMEOUT_SECONDS)


def _deliver_result(future: asyncio.Future[bool], approved: bool) -> None:
    """Resolve a waiter's future on its own loop, tolerating a late race."""
    if not future.done():
        future.set_result(bool(approved))


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    TOO_MANY_PENDING = "too_many_pending"


@dataclass
class ApprovalRequest:
    """A pending action awaiting explicit user confirmation."""

    token: str = field(default_factory=lambda: str(uuid.uuid4()))
    tool_call_id: str = ""
    tool_name: str = ""
    command: str = ""
    arguments: list[str] = field(default_factory=list)
    working_directory: str = ""
    reason: str = ""
    turn_id: str = ""
    approval_kind: str = ""
    details: tuple[tuple[str, Any], ...] = ()
    requires_tool_call_id: bool = False
    timeout_seconds: float = 0.0
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: ApprovalStatus = ApprovalStatus.PENDING
    future: Optional[asyncio.Future[bool]] = None
    notified: bool = field(default=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "command": self.command,
            "arguments": list(self.arguments),
            "working_directory": self.working_directory,
            "reason": self.reason,
            "turn_id": self.turn_id,
            "approval_kind": self.approval_kind,
            "details": dict(self.details),
            "requires_tool_call_id": self.requires_tool_call_id,
            "timeout_seconds": round(self.timeout_seconds, 3),
            "created_at": round(self.created_at, 3),
            "expires_at": round(self.expires_at, 3),
            "status": self.status.value,
        }


class ApprovalGate:
    """Coordinates one-time authorization tokens and user decisions."""

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        on_approval_requested: Optional[Callable[[dict[str, Any]], None]] = None,
        on_approval_resolved: Optional[Callable[[dict[str, Any]], None]] = None,
        max_pending: int = MAX_PENDING_APPROVALS,
        max_consumed_tokens: int = MAX_CONSUMED_TOKENS,
    ):
        try:
            configured_timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            configured_timeout = DEFAULT_APPROVAL_TIMEOUT_SECONDS
        # Preserve the historical floor while bounding an oversized or
        # non-finite timeout. ``max`` keeps NaN from producing NaN.
        self._timeout_seconds = min(
            max(0.01, configured_timeout),
            MAX_APPROVAL_TIMEOUT_SECONDS,
        )
        self._on_approval_requested = on_approval_requested
        self._on_approval_resolved = on_approval_resolved
        self._max_pending = max(1, int(max_pending))
        self._lock = threading.Lock()
        self._pending: dict[str, ApprovalRequest] = {}
        # A deque keeps the ledger bounded while preserving membership tests.
        self._consumed_tokens: collections.deque[str] = collections.deque(
            maxlen=max(1, int(max_consumed_tokens))
        )

    # ── Request lifecycle ──────────────────────────────────────────────────

    async def request_approval(
        self,
        tool_call_id: str,
        tool_name: str,
        command: str,
        arguments: list[str],
        working_directory: str,
        reason: str,
        turn_id: str = "",
        *,
        approval_kind: str | ApprovalKind = ApprovalKind.UNSPECIFIED,
        details: Optional[Mapping[str, Any]] = None,
        require_tool_call_id: bool = False,
        timeout_seconds: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Create a pending approval request and await human confirmation.

        Returns ``(True, "approved")`` if the user confirms, or
        ``(False, reason)`` if rejected, expired, cancelled, refused because
        too many approvals are already pending, or malformed.

        New keyword arguments are optional so terminal approvals remain
        backward-compatible.  Mutation callers pass a bounded ``details``
        mapping, ``require_tool_call_id=True``, and an approval kind.
        """
        # Validate every new field before a future or pending request exists.
        # A malformed request must never become approve-capable.
        try:
            clean_kind = normalize_approval_kind(approval_kind)
            frozen_details = normalize_approval_details(details)
            request_timeout = normalize_request_timeout(timeout_seconds)
        except ValueError:
            return False, "invalid_approval_request"

        if not isinstance(tool_call_id, str):
            return False, "invalid_approval_request"
        clean_tool_call_id = tool_call_id.strip()
        if require_tool_call_id and not clean_tool_call_id:
            return False, "invalid_approval_request"
        if not isinstance(tool_name, str) or not tool_name.strip():
            return False, "invalid_approval_request"
        clean_tool_name = tool_name.strip()
        if not isinstance(command, str) or not command.strip():
            return False, "invalid_approval_request"
        if not isinstance(reason, str):
            return False, "invalid_approval_request"
        if not isinstance(arguments, list) or any(
            not isinstance(item, str) for item in arguments
        ):
            return False, "invalid_approval_request"
        if not isinstance(working_directory, str):
            return False, "invalid_approval_request"
        if not isinstance(turn_id, str):
            return False, "invalid_approval_request"

        effective_timeout = (
            request_timeout
            if request_timeout is not None
            else self._timeout_seconds
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()

        now = time.time()
        req = ApprovalRequest(
            tool_call_id=clean_tool_call_id,
            tool_name=clean_tool_name,
            command=command.strip(),
            arguments=list(arguments),
            working_directory=working_directory,
            reason=reason,
            turn_id=turn_id.strip(),
            approval_kind=clean_kind,
            details=frozen_details,
            requires_tool_call_id=bool(require_tool_call_id),
            timeout_seconds=effective_timeout,
            created_at=now,
            expires_at=now + effective_timeout,
            future=future,
        )

        with self._lock:
            if len(self._pending) >= self._max_pending:
                req.status = ApprovalStatus.TOO_MANY_PENDING
                return False, "too_many_pending"
            self._pending[req.token] = req

        self._notify_requested(req)

        try:
            approved = await asyncio.wait_for(future, timeout=effective_timeout)
        except asyncio.TimeoutError:
            with self._lock:
                finalized = self._finalize_locked(req, ApprovalStatus.EXPIRED)
                status = req.status
            if finalized:
                self._notify_resolved(req)
            if status == ApprovalStatus.CANCELLED:
                raise asyncio.CancelledError
            if status == ApprovalStatus.EXPIRED:
                return False, "expired"
            # A human decision can win the race against the timer.
            return status == ApprovalStatus.APPROVED, status.value

        except asyncio.CancelledError:
            with self._lock:
                finalized = self._finalize_locked(req, ApprovalStatus.CANCELLED)
            if finalized:
                self._notify_resolved(req)
            raise

        with self._lock:
            if req.status == ApprovalStatus.PENDING:
                target = (
                    ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
                )
                finalized = self._finalize_locked(req, target)
            else:
                finalized = False
            status = req.status
        if finalized:
            self._notify_resolved(req)
        return status == ApprovalStatus.APPROVED, status.value

    def resolve(
        self,
        token: str,
        approved: bool,
        turn_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Resolve a pending confirmation request by token.

        Tokens are single-use only; subsequent attempts fail. A request that
        belongs to a conversation turn can only be resolved by a caller that
        presents that exact turn identifier. Supplying no turn identifier for a
        bound request is refused, and an unbound request cannot be resolved
        through a turn-bound workflow.
        """
        clean_token = (token or "").strip()
        if not clean_token:
            return False, "Approval token cannot be empty."
        if not isinstance(approved, bool):
            return False, "Approval decision must be a boolean."
        supplied_turn = turn_id.strip() if isinstance(turn_id, str) else ""

        now = time.time()
        with self._lock:
            if clean_token in self._consumed_tokens:
                return (
                    False,
                    "Approval token has already been consumed and cannot be reused.",
                )

            req = self._pending.get(clean_token)
            if req is None:
                return False, "Approval request not found or already closed."

            if req.turn_id:
                if not supplied_turn or req.turn_id != supplied_turn:
                    return (
                        False,
                        "Approval request is bound to a different conversation turn.",
                    )
            elif supplied_turn:
                return (
                    False,
                    "Approval request is not bound to a conversation turn and "
                    "cannot be resolved in a turn-bound workflow.",
                )

            supplied_call = (
                tool_call_id.strip() if isinstance(tool_call_id, str) else ""
            )
            if req.requires_tool_call_id:
                if not supplied_call:
                    return (
                        False,
                        "Approval request requires the exact tool_call_id that "
                        "created it.",
                    )
                if req.tool_call_id != supplied_call:
                    return (
                        False,
                        "Approval request is bound to a different tool call.",
                    )
            elif supplied_call and req.tool_call_id != supplied_call:
                return False, "Approval request is bound to a different tool call."

            if now > req.expires_at:
                finalized = self._finalize_locked(req, ApprovalStatus.EXPIRED)
                status = ApprovalStatus.EXPIRED
            else:
                target = (
                    ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
                )
                finalized = self._finalize_locked(req, target)
                status = req.status
            future = req.future

        if finalized:
            self._notify_resolved(req)

        if future is not None and not future.done():
            try:
                future.get_loop().call_soon_threadsafe(
                    _deliver_result,
                    future,
                    bool(approved),
                )
            except RuntimeError:
                pass

        if status == ApprovalStatus.EXPIRED:
            return False, "Approval request has expired."
        if not finalized:
            return False, "Request is no longer active."
        return True, "Approval resolved successfully."

    def cancel_all(
        self,
        tool_call_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        """Cancel pending requests, optionally filtered by tool call or turn."""
        cancelled: list[ApprovalRequest] = []
        with self._lock:
            for token, req in list(self._pending.items()):
                if tool_call_id is not None and req.tool_call_id != tool_call_id:
                    continue
                if turn_id is not None and req.turn_id != turn_id:
                    continue
                if self._finalize_locked(req, ApprovalStatus.CANCELLED):
                    cancelled.append(req)

        for req in cancelled:
            self._notify_resolved(req)
            future = req.future
            if future is not None and not future.done():
                try:
                    future.get_loop().call_soon_threadsafe(future.cancel)
                except RuntimeError:
                    pass

    def get_pending_requests(self) -> list[dict[str, Any]]:
        """Return all currently open, unexpired approval requests."""
        now = time.time()
        with self._lock:
            return [
                req.to_dict()
                for req in self._pending.values()
                if req.status == ApprovalStatus.PENDING and req.expires_at > now
            ]

    # ── Internals ──────────────────────────────────────────────────────────

    def _finalize_locked(
        self,
        req: ApprovalRequest,
        status: ApprovalStatus,
    ) -> bool:
        """Move a request out of pending exactly once. Caller holds the lock."""
        if req.status != ApprovalStatus.PENDING:
            return False
        req.status = status
        self._pending.pop(req.token, None)
        self._consumed_tokens.append(req.token)
        return True

    def _notify_requested(self, req: ApprovalRequest) -> None:
        if self._on_approval_requested is None:
            return
        try:
            self._on_approval_requested(req.to_dict())
        except Exception:
            pass

    def _notify_resolved(self, req: ApprovalRequest) -> None:
        with self._lock:
            if req.notified:
                return
            req.notified = True
        if self._on_approval_resolved is None:
            return
        try:
            self._on_approval_resolved(req.to_dict())
        except Exception:
            pass


__all__ = [
    "ApprovalGate",
    "ApprovalKind",
    "ApprovalRequest",
    "ApprovalStatus",
    "DEFAULT_APPROVAL_TIMEOUT_SECONDS",
    "MAX_APPROVAL_DETAILS",
    "MAX_APPROVAL_DETAILS_BYTES",
    "MAX_APPROVAL_TIMEOUT_SECONDS",
    "MAX_CONSUMED_TOKENS",
    "MAX_PENDING_APPROVALS",
    "normalize_approval_details",
    "normalize_approval_kind",
    "normalize_request_timeout",
]
