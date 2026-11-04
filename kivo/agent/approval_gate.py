"""Asynchronous human-in-the-loop approval gate for sensitive agent actions."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 60.0


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


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
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: ApprovalStatus = ApprovalStatus.PENDING
    future: Optional[asyncio.Future[bool]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "command": self.command,
            "arguments": list(self.arguments),
            "working_directory": self.working_directory,
            "reason": self.reason,
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
    ):
        self._timeout_seconds = max(5.0, timeout_seconds)
        self._on_approval_requested = on_approval_requested
        self._on_approval_resolved = on_approval_resolved
        self._lock = threading.Lock()
        self._pending: dict[str, ApprovalRequest] = {}
        self._consumed_tokens: set[str] = set()

    async def request_approval(
        self,
        tool_call_id: str,
        tool_name: str,
        command: str,
        arguments: list[str],
        working_directory: str,
        reason: str,
    ) -> tuple[bool, str]:
        """Create a pending approval request and await human confirmation.

        Returns (True, "approved") if user confirms, or (False, reason) if rejected/expired.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()

        now = time.time()
        req = ApprovalRequest(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            command=command,
            arguments=list(arguments),
            working_directory=working_directory,
            reason=reason,
            created_at=now,
            expires_at=now + self._timeout_seconds,
            future=future,
        )

        with self._lock:
            self._pending[req.token] = req

        # Notify UI through IPC callback
        if self._on_approval_requested is not None:
            try:
                self._on_approval_requested(req.to_dict())
            except Exception:
                pass

        try:
            approved = await asyncio.wait_for(future, timeout=self._timeout_seconds)
            with self._lock:
                self._pending.pop(req.token, None)
                self._consumed_tokens.add(req.token)
                req.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED

            # Notify UI that request is resolved
            if self._on_approval_resolved is not None:
                try:
                    self._on_approval_resolved(req.to_dict())
                except Exception:
                    pass

            if approved:
                return True, "approved"
            return False, "rejected"

        except asyncio.TimeoutError:
            with self._lock:
                self._pending.pop(req.token, None)
                self._consumed_tokens.add(req.token)
                req.status = ApprovalStatus.EXPIRED

            if self._on_approval_resolved is not None:
                try:
                    self._on_approval_resolved(req.to_dict())
                except Exception:
                    pass
            return False, "expired"

        except asyncio.CancelledError:
            with self._lock:
                self._pending.pop(req.token, None)
                self._consumed_tokens.add(req.token)
                req.status = ApprovalStatus.CANCELLED

            if self._on_approval_resolved is not None:
                try:
                    self._on_approval_resolved(req.to_dict())
                except Exception:
                    pass
            return False, "cancelled"

    def resolve(self, token: str, approved: bool) -> tuple[bool, str]:
        """Resolve a pending confirmation request by token.

        Tokens are single-use only; subsequent attempts fail.
        """
        clean_token = (token or "").strip()
        with self._lock:
            if clean_token in self._consumed_tokens:
                return False, "Approval token has already been consumed and cannot be reused."

            req = self._pending.get(clean_token)
            if req is None:
                return False, "Approval request not found or already closed."

            if time.time() > req.expires_at:
                self._pending.pop(clean_token, None)
                self._consumed_tokens.add(clean_token)
                req.status = ApprovalStatus.EXPIRED
                if req.future and not req.future.done():
                    req.future.set_result(False)
                return False, "Approval request has expired."

        # Complete the waiting future on the loop
        if req.future and not req.future.done():
            loop = req.future.get_loop()
            loop.call_soon_threadsafe(req.future.set_result, bool(approved))
            return True, "Approval resolved successfully."

        return False, "Request is no longer active."

    def cancel_all(self, tool_call_id: Optional[str] = None) -> None:
        """Cancel pending requests (e.g. on turn cancellation or backend shutdown)."""
        with self._lock:
            tokens = list(self._pending.keys())
            for t in tokens:
                req = self._pending[t]
                if tool_call_id is None or req.tool_call_id == tool_call_id:
                    self._pending.pop(t, None)
                    self._consumed_tokens.add(t)
                    req.status = ApprovalStatus.CANCELLED
                    if req.future and not req.future.done():
                        loop = req.future.get_loop()
                        loop.call_soon_threadsafe(req.future.cancel)

    def get_pending_requests(self) -> list[dict[str, Any]]:
        """Return all currently open approval requests."""
        now = time.time()
        with self._lock:
            active = []
            for req in self._pending.values():
                if req.expires_at > now:
                    active.append(req.to_dict())
            return active
