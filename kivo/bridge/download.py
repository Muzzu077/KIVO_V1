"""Authenticated desktop-to-Android download tickets and chunk streaming.

Security properties
-------------------
* A ticket is created only after the desktop user explicitly authorizes one
  File Shelf item for export.  Staging alone grants nothing.
* Ticket identifiers are 256-bit random capabilities bound to one paired
  session, device, file identity, size, SHA-256, and logical transfer.
* The Android client never supplies a path.  Requests are authenticated with
  the existing ``K_up`` transcript (ticket/chunk identifiers are part of the
  canonical request path) and responses with ``K_down``.
* The authorized file descriptor is retained for the whole logical transfer.
  Chunks are read with ``pread`` at deterministic offsets; short reads or any
  device/inode/size/mtime/mount change abort the download.
* The full file is hashed before issuance and re-hashed in chunk order while
  streaming.  Completion requires the incremental digest to match.
* At most two concurrent downloads, a bounded ticket registry, five-minute
  ticket expiry, thirty-second idle timeout, and explicit cleanup on expiry,
  revocation, cancellation, or shutdown.
* Downloads use no quarantine reservation and never modify the source file.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from kivo.bridge.download_validation import (
    DOWNLOAD_ACTIVE_LEASE_SECONDS,
    DOWNLOAD_CHUNK_BYTES,
    DOWNLOAD_IDLE_TIMEOUT_SECONDS,
    DOWNLOAD_TICKET_TTL_SECONDS,
    MAX_ACTIVE_DOWNLOADS_PER_SESSION,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_DOWNLOAD_BYTES,
    MAX_DOWNLOAD_REGISTRY,
    TERMINAL_RETENTION_SECONDS,
    DownloadValidationError,
    chunk_length,
    total_chunks,
    validate_chunk_index,
    validate_download_id,
    validate_download_size,
)
from kivo.bridge.models import PairedSession
from kivo.files.service import _get_mount_id
from kivo.shelf.models import FileIdentity
from kivo.shelf.service import ShelfExportGrant


class DownloadState(str, Enum):
    ISSUED = "issued"
    CLAIMED = "claimed"
    STREAMING = "streaming"
    COMPLETED = "completed"
    EXPIRED = "expired"
    REVOKED = "revoked"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_DOWNLOAD_STATES = frozenset(
    {
        DownloadState.COMPLETED,
        DownloadState.EXPIRED,
        DownloadState.REVOKED,
        DownloadState.FAILED,
        DownloadState.CANCELLED,
    }
)
ACTIVE_DOWNLOAD_STATES = frozenset({DownloadState.CLAIMED, DownloadState.STREAMING})


class DownloadError(Exception):
    """A structured download refusal with an HTTP status suggestion."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.http_status = int(http_status)
        self.details = details or {}
        super().__init__(message)


@dataclass
class DownloadTicket:
    download_id: str
    session_id: str
    device_id: str
    device_name: str
    item_id: str
    filename: str
    file_type: str
    size: int
    sha256: bytes
    identity: FileIdentity
    state: DownloadState = DownloadState.ISSUED
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    active_expires_at: float = 0.0
    claimed_at: float = 0.0
    last_activity: float = 0.0
    bytes_sent: int = 0
    next_chunk_index: int = 0
    error_code: str | None = None
    grant: ShelfExportGrant | None = field(default=None, repr=False)
    chunk_digests: list[bytes] = field(default_factory=list, repr=False)
    stream_hasher: Any = field(default_factory=hashlib.sha256, repr=False)

    @property
    def total_chunks(self) -> int:
        return total_chunks(self.size)


@dataclass(frozen=True)
class DownloadChunk:
    download_id: str
    chunk_index: int
    offset: int
    data: bytes = field(repr=False)
    sha256: bytes
    state: str
    next_chunk_index: int
    bytes_sent: int
    size: int
    total_chunks: int


def _progress_percent(ticket: DownloadTicket) -> int:
    if ticket.size == 0:
        return 100 if ticket.state == DownloadState.COMPLETED else 0
    if ticket.bytes_sent >= ticket.size:
        return 100
    return int((ticket.bytes_sent * 100) // ticket.size)


def effective_expires_at(ticket: DownloadTicket) -> float:
    """Return the deadline that currently governs the ticket.

    The five-minute value remains the redemption deadline.  Once a ticket has
    been claimed, the separately bounded active lease governs chunk streaming
    and completion; terminal tickets report their original redemption deadline.
    """
    if ticket.active_expires_at > 0.0 and ticket.state not in TERMINAL_DOWNLOAD_STATES:
        return ticket.active_expires_at
    return ticket.expires_at


def public_download(ticket: DownloadTicket) -> dict[str, Any]:
    """Return only path-free metadata safe for IPC, events, and the LAN API."""
    return {
        "download_id": ticket.download_id,
        "device_id": ticket.device_id,
        "filename": ticket.filename,
        "file_type": ticket.file_type,
        "size": ticket.size,
        "sha256": ticket.sha256.hex(),
        "bytes_sent": ticket.bytes_sent,
        "progress_percent": _progress_percent(ticket),
        "state": ticket.state.value,
        "device_name": ticket.device_name,
        "chunk_size": DOWNLOAD_CHUNK_BYTES,
        "next_chunk_index": ticket.next_chunk_index,
        "total_chunks": ticket.total_chunks,
        "created_at": round(ticket.created_at, 3),
        "updated_at": round(ticket.updated_at, 3),
        "expires_at": round(effective_expires_at(ticket), 3),
        "claim_expires_at": round(ticket.expires_at, 3),
        "active_expires_at": round(ticket.active_expires_at, 3),
        "error_code": ticket.error_code,
    }


def _pread_exact(fd: int, length: int, offset: int) -> bytes:
    """Read exactly ``length`` bytes at ``offset`` or raise ``OSError``."""
    parts: list[bytes] = []
    remaining = length
    position = offset
    while remaining > 0:
        data = os.pread(fd, remaining, position)
        if not data:
            raise OSError("unexpected end of file while reading download source")
        parts.append(data)
        position += len(data)
        remaining -= len(data)
    return b"".join(parts)


def _stat_signature(st: os.stat_result, mount_id: int | None) -> tuple[int, int, int, int, int, int | None]:
    return (
        int(st.st_dev),
        int(st.st_ino),
        int(st.st_size),
        int(st.st_mtime_ns),
        int(st.st_mode),
        mount_id,
    )


def _identity_signature(identity: FileIdentity) -> tuple[int, int, int, int, int, int | None]:
    return (
        int(identity.dev),
        int(identity.ino),
        int(identity.size),
        int(identity.mtime_ns),
        int(identity.mode),
        identity.mnt_id,
    )


def _fd_signature(fd: int) -> tuple[int, int, int, int, int, int | None]:
    return _stat_signature(os.fstat(fd), _get_mount_id(fd))


class DownloadManager:
    """Single owner of authorized download tickets and retained descriptors."""

    def __init__(
        self,
        *,
        session_provider: Callable[[], PairedSession | None],
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
        cleanup_interval: float = 5.0,
    ):
        self._session_provider = session_provider
        self._on_event = on_event
        self._clock = clock
        self._cleanup_interval = max(0.1, float(cleanup_interval))
        self._lock = threading.RLock()
        self._tickets: dict[str, DownloadTicket] = {}
        self._started = False
        self._closing = False
        self._closed = False
        self._stop_event = threading.Event()
        self._cleanup_thread: threading.Thread | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise DownloadError(
                    "download_unavailable", "The download manager is closed.", http_status=503
                )
            if self._started:
                return
            self._closing = False
            self._started = True
            self._stop_event.clear()
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name="kivo-download-cleanup",
                daemon=True,
            )
            self._cleanup_thread.start()

    def stop(self, reason: str = "bridge_stopped") -> None:
        with self._lock:
            self._closing = True
            self._cancel_all_locked(reason)
            self._stop_event.set()
        thread = self._cleanup_thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=2.0)
        with self._lock:
            self._cleanup_thread = None
            self._started = False

    def close(self, reason: str = "bridge_stopped") -> None:
        with self._lock:
            if self._closed:
                return
        self.stop(reason=reason)
        with self._lock:
            self._closed = True
            self._cancel_all_locked(reason)
            self._tickets.clear()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._started and not self._closed

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(self._cleanup_interval):
            try:
                self.cleanup_expired()
            except Exception:
                continue

    # ── Ticket issuance ────────────────────────────────────────────────────

    def create_ticket(self, grant: ShelfExportGrant) -> DownloadTicket:
        """Hash an explicitly authorized file and issue a bound ticket.

        Ownership of ``grant`` always transfers to this method, including on
        failure.  The full source is hashed with bounded memory before the
        ticket becomes visible to any client.
        """
        owned = False
        try:
            if grant.closed:
                raise DownloadError(
                    "download_source_unavailable",
                    "The authorized file descriptor is closed.",
                    http_status=503,
                )
            try:
                size = validate_download_size(grant.size_bytes)
            except DownloadValidationError as exc:
                raise DownloadError(exc.code, exc.message, http_status=413) from exc

            session = self._current_session()
            if session is None:
                raise DownloadError(
                    "no_active_session",
                    "A paired device is required before a download can be authorized.",
                    http_status=409,
                )

            digest = self._hash_source(grant)
            now = self._clock()

            with self._lock:
                if not self._started or self._closing or self._closed:
                    raise DownloadError(
                        "download_unavailable", "Downloads are not available.", http_status=503
                    )
                current = self._current_session()
                if (
                    current is None
                    or current.session_id != session.session_id
                    or current.is_expired(now)
                ):
                    raise DownloadError(
                        "session_expired",
                        "The paired session is no longer valid.",
                        http_status=401,
                    )

                self._prune_locked(now)
                active_for_session = sum(
                    1
                    for ticket in self._tickets.values()
                    if ticket.session_id == session.session_id
                    and ticket.state not in TERMINAL_DOWNLOAD_STATES
                )
                if active_for_session >= MAX_ACTIVE_DOWNLOADS_PER_SESSION:
                    raise DownloadError(
                        "session_download_limit",
                        "This device already has the maximum number of active download tickets.",
                        http_status=429,
                    )
                if len(self._tickets) >= MAX_DOWNLOAD_REGISTRY:
                    raise DownloadError(
                        "download_registry_full",
                        "The download registry is currently full.",
                        http_status=429,
                    )

                download_id = "tkt_" + secrets.token_hex(32)
                while download_id in self._tickets:
                    download_id = "tkt_" + secrets.token_hex(32)

                ticket = DownloadTicket(
                    download_id=download_id,
                    session_id=session.session_id,
                    device_id=session.client_id,
                    device_name=session.device_name,
                    item_id=grant.item_id,
                    filename=grant.name,
                    file_type=grant.file_type,
                    size=size,
                    sha256=digest,
                    identity=grant.identity,
                    state=DownloadState.ISSUED,
                    created_at=now,
                    updated_at=now,
                    expires_at=now + DOWNLOAD_TICKET_TTL_SECONDS,
                    last_activity=now,
                    grant=grant,
                )
                self._tickets[download_id] = ticket
                owned = True
                self._emit_locked("bridge_download_requested", ticket)
                return ticket
        finally:
            if not owned:
                grant.close()

    def _hash_source(self, grant: ShelfExportGrant) -> bytes:
        """Hash the retained descriptor and prove the source stayed stable."""
        fd = grant.fd
        try:
            before = _fd_signature(fd)
        except OSError as exc:
            raise DownloadError(
                "download_source_unavailable",
                "The authorized file is no longer readable.",
                http_status=503,
            ) from exc
        if before != _identity_signature(grant.identity):
            raise DownloadError(
                "download_source_changed",
                "The file changed after it was authorized for export.",
                http_status=422,
            )
        if before[2] != grant.size_bytes or before[2] > MAX_DOWNLOAD_BYTES:
            raise DownloadError(
                "download_source_changed",
                "The file size no longer matches the authorized metadata.",
                http_status=422,
            )
        if not stat.S_ISREG(before[4]):
            raise DownloadError(
                "download_source_unavailable",
                "The authorized source is not a regular file.",
                http_status=422,
            )

        hasher = hashlib.sha256()
        offset = 0
        try:
            while offset < grant.size_bytes:
                length = min(DOWNLOAD_CHUNK_BYTES, grant.size_bytes - offset)
                hasher.update(_pread_exact(fd, length, offset))
                offset += length
            after = _fd_signature(fd)
        except OSError as exc:
            raise DownloadError(
                "download_source_changed",
                "The file changed while it was being hashed for the download ticket.",
                http_status=422,
            ) from exc

        if after != before:
            raise DownloadError(
                "download_source_changed",
                "The file changed while it was being hashed for the download ticket.",
                http_status=422,
            )
        return hasher.digest()

    # ── Client operations ──────────────────────────────────────────────────

    def claim(self, session: PairedSession, download_id: str) -> DownloadTicket:
        """Atomically claim an issued ticket for its bound session/device.

        Repeated claims by the same session and device are idempotent and
        resume the same logical transfer.  No other session or device can
        claim it.
        """
        safe_id = self._validate_id(download_id)
        now = self._clock()
        with self._lock:
            ticket = self._get_bound_locked(session, safe_id, now)
            if ticket.state == DownloadState.COMPLETED:
                return ticket
            if ticket.state in TERMINAL_DOWNLOAD_STATES:
                raise self._terminal_error(ticket)
            if ticket.state in ACTIVE_DOWNLOAD_STATES:
                if now >= effective_expires_at(ticket):
                    self._finish_locked(
                        ticket, DownloadState.EXPIRED, error_code="active_lease_expired"
                    )
                    raise DownloadError(
                        "download_expired", "The active download lease has expired.", http_status=410
                    )
                return ticket
            if ticket.expires_at <= now:
                self._finish_locked(ticket, DownloadState.EXPIRED, error_code="ticket_expired")
                raise DownloadError(
                    "download_expired", "The download ticket has expired.", http_status=410
                )

            concurrent = sum(
                1 for item in self._tickets.values() if item.state in ACTIVE_DOWNLOAD_STATES
            )
            if concurrent >= MAX_CONCURRENT_DOWNLOADS:
                raise DownloadError(
                    "concurrent_download_limit",
                    "The maximum number of concurrent downloads is already active.",
                    http_status=429,
                )
            ticket.state = DownloadState.CLAIMED
            ticket.claimed_at = now
            lease = now + DOWNLOAD_ACTIVE_LEASE_SECONDS
            if session.expires_at > 0.0:
                lease = min(lease, session.expires_at)
            ticket.active_expires_at = lease
            ticket.updated_at = now
            ticket.last_activity = now
            ticket.error_code = None
            self._emit_locked("bridge_download_progress", ticket)
            return ticket

    def get(self, session: PairedSession, download_id: str) -> DownloadTicket:
        safe_id = self._validate_id(download_id)
        now = self._clock()
        with self._lock:
            ticket = self._get_bound_locked(session, safe_id, now)
            if (
                ticket.state not in TERMINAL_DOWNLOAD_STATES
                and now >= effective_expires_at(ticket)
            ):
                code = (
                    "active_lease_expired"
                    if ticket.state in ACTIVE_DOWNLOAD_STATES
                    else "ticket_expired"
                )
                self._finish_locked(ticket, DownloadState.EXPIRED, error_code=code)
            return ticket

    def get_by_id(self, download_id: str) -> DownloadTicket:
        """Desktop-side unscoped lookup for diagnostics and tests."""
        safe_id = self._validate_id(download_id)
        with self._lock:
            self._prune_locked(self._clock())
            ticket = self._tickets.get(safe_id)
            if ticket is None:
                raise DownloadError(
                    "download_not_found", "The download ticket does not exist.", http_status=404
                )
            return ticket

    def read_chunk(
        self, session: PairedSession, download_id: str, chunk_index: object
    ) -> DownloadChunk:
        safe_id = self._validate_id(download_id)
        try:
            index = validate_chunk_index(chunk_index)
        except DownloadValidationError as exc:
            raise DownloadError(exc.code, exc.message, http_status=400) from exc

        now = self._clock()
        with self._lock:
            ticket = self._get_bound_locked(session, safe_id, now)
            if ticket.state == DownloadState.ISSUED:
                raise DownloadError(
                    "download_not_claimed",
                    "The download ticket must be claimed before chunks are requested.",
                    http_status=409,
                )
            if ticket.state == DownloadState.COMPLETED:
                raise DownloadError(
                    "download_completed",
                    "The download has already completed.",
                    http_status=409,
                    details={"state": ticket.state.value},
                )
            if ticket.state in TERMINAL_DOWNLOAD_STATES:
                raise self._terminal_error(ticket)
            if now >= effective_expires_at(ticket):
                code = (
                    "active_lease_expired"
                    if ticket.active_expires_at > 0.0
                    else "ticket_expired"
                )
                self._finish_locked(ticket, DownloadState.EXPIRED, error_code=code)
                raise DownloadError(
                    "download_expired", "The download ticket has expired.", http_status=410
                )
            if now - ticket.last_activity > DOWNLOAD_IDLE_TIMEOUT_SECONDS:
                self._finish_locked(ticket, DownloadState.FAILED, error_code="idle_timeout")
                raise DownloadError(
                    "download_idle_timeout",
                    "The download was idle beyond the accepted timeout.",
                    http_status=408,
                )

            try:
                expected_length = chunk_length(ticket.size, index)
            except DownloadValidationError as exc:
                raise DownloadError(
                    exc.code,
                    exc.message,
                    http_status=400,
                    details={"total_chunks": ticket.total_chunks},
                ) from exc
            offset = index * DOWNLOAD_CHUNK_BYTES

            if index > ticket.next_chunk_index:
                raise DownloadError(
                    "chunk_out_of_order",
                    "Chunks must be requested in order.",
                    http_status=409,
                    details={
                        "expected_chunk_index": ticket.next_chunk_index,
                        "received_bytes": ticket.bytes_sent,
                    },
                )

            self._ensure_source_locked(ticket)
            if ticket.grant is None:
                raise DownloadError(
                    "download_source_unavailable",
                    "The download source is no longer available.",
                    http_status=503,
                )
            try:
                data = _pread_exact(ticket.grant.fd, expected_length, offset)
            except OSError as exc:
                self._source_failed_locked(ticket, "download_source_changed")
                raise DownloadError(
                    "download_source_changed",
                    "The source file changed while the chunk was being read.",
                    http_status=422,
                ) from exc
            self._ensure_source_locked(ticket)

            digest = hashlib.sha256(data).digest()
            if index < ticket.next_chunk_index:
                previous = ticket.chunk_digests[index]
                if not hmac.compare_digest(previous, digest):
                    self._source_failed_locked(ticket, "download_source_changed")
                    raise DownloadError(
                        "download_source_changed",
                        "The resent chunk no longer matches the authorized source.",
                        http_status=422,
                    )
            else:
                ticket.chunk_digests.append(digest)
                ticket.stream_hasher.update(data)
                ticket.next_chunk_index = index + 1
                ticket.bytes_sent += len(data)

            if ticket.state == DownloadState.CLAIMED:
                ticket.state = DownloadState.STREAMING
            ticket.updated_at = now
            ticket.last_activity = now
            self._emit_locked("bridge_download_progress", ticket)
            return DownloadChunk(
                download_id=ticket.download_id,
                chunk_index=index,
                offset=offset,
                data=data,
                sha256=digest,
                state=ticket.state.value,
                next_chunk_index=ticket.next_chunk_index,
                bytes_sent=ticket.bytes_sent,
                size=ticket.size,
                total_chunks=ticket.total_chunks,
            )

    def complete(self, session: PairedSession, download_id: str) -> DownloadTicket:
        safe_id = self._validate_id(download_id)
        now = self._clock()
        with self._lock:
            ticket = self._get_bound_locked(session, safe_id, now)
            if ticket.state == DownloadState.COMPLETED:
                return ticket
            if ticket.state in TERMINAL_DOWNLOAD_STATES:
                raise self._terminal_error(ticket)
            if now >= effective_expires_at(ticket):
                code = (
                    "active_lease_expired"
                    if ticket.active_expires_at > 0.0
                    else "ticket_expired"
                )
                self._finish_locked(ticket, DownloadState.EXPIRED, error_code=code)
                raise DownloadError(
                    "download_expired", "The download ticket has expired.", http_status=410
                )
            if ticket.state == DownloadState.ISSUED:
                raise DownloadError(
                    "download_not_claimed",
                    "The download ticket must be claimed before completion.",
                    http_status=409,
                )
            if ticket.next_chunk_index != ticket.total_chunks or ticket.bytes_sent != ticket.size:
                raise DownloadError(
                    "download_incomplete",
                    "The download is not complete.",
                    http_status=409,
                    details={
                        "next_chunk_index": ticket.next_chunk_index,
                        "total_chunks": ticket.total_chunks,
                        "bytes_sent": ticket.bytes_sent,
                        "size": ticket.size,
                    },
                )
            self._ensure_source_locked(ticket)
            if not hmac.compare_digest(ticket.stream_hasher.digest(), ticket.sha256):
                self._finish_locked(
                    ticket, DownloadState.FAILED, error_code="download_integrity_failed"
                )
                raise DownloadError(
                    "download_integrity_failed",
                    "The streamed bytes do not match the authorized file digest.",
                    http_status=422,
                )
            self._finish_locked(ticket, DownloadState.COMPLETED, error_code=None)
            return ticket

    def cancel(self, session: PairedSession, download_id: str) -> DownloadTicket:
        safe_id = self._validate_id(download_id)
        now = self._clock()
        with self._lock:
            ticket = self._get_bound_locked(session, safe_id, now)
            if ticket.state in TERMINAL_DOWNLOAD_STATES:
                return ticket
            self._finish_locked(ticket, DownloadState.CANCELLED, error_code="client_cancelled")
            return ticket

    # ── Desktop operations ─────────────────────────────────────────────────

    def cancel_desktop(self, download_id: str) -> DownloadTicket:
        safe_id = self._validate_id(download_id)
        with self._lock:
            self._prune_locked(self._clock())
            ticket = self._tickets.get(safe_id)
            if ticket is None:
                raise DownloadError(
                    "download_not_found", "The download ticket does not exist.", http_status=404
                )
            if ticket.state in TERMINAL_DOWNLOAD_STATES:
                return ticket
            self._finish_locked(ticket, DownloadState.CANCELLED, error_code="desktop_cancelled")
            return ticket

    def list_public(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._prune_locked(self._clock())
            tickets = sorted(self._tickets.values(), key=lambda item: item.created_at)
            return [
                public_download(ticket)
                for ticket in tickets
                if session_id is None or ticket.session_id == session_id
            ]

    def cancel_session(self, session_id: str, reason: str = "session_revoked") -> int:
        cancelled = 0
        with self._lock:
            for ticket in list(self._tickets.values()):
                if ticket.session_id == session_id and ticket.state not in TERMINAL_DOWNLOAD_STATES:
                    self._finish_locked(ticket, DownloadState.REVOKED, error_code=reason)
                    cancelled += 1
        return cancelled

    def cancel_all(self, reason: str = "bridge_stopped") -> int:
        with self._lock:
            return self._cancel_all_locked(reason)

    def _cancel_all_locked(self, reason: str) -> int:
        cancelled = 0
        for ticket in list(self._tickets.values()):
            if ticket.state not in TERMINAL_DOWNLOAD_STATES:
                self._finish_locked(ticket, DownloadState.CANCELLED, error_code=reason)
                cancelled += 1
        return cancelled

    # ── Cleanup ────────────────────────────────────────────────────────────

    def cleanup_expired(self) -> None:
        now = self._clock()
        with self._lock:
            for ticket in list(self._tickets.values()):
                if ticket.state in TERMINAL_DOWNLOAD_STATES:
                    continue
                if not self._session_valid_locked(ticket.session_id, now):
                    self._finish_locked(
                        ticket, DownloadState.REVOKED, error_code="session_revoked"
                    )
                    continue
                if now >= effective_expires_at(ticket):
                    code = (
                        "active_lease_expired"
                        if ticket.state in ACTIVE_DOWNLOAD_STATES
                        else "ticket_expired"
                    )
                    self._finish_locked(ticket, DownloadState.EXPIRED, error_code=code)
                    continue
                if (
                    ticket.state in ACTIVE_DOWNLOAD_STATES
                    and now - ticket.last_activity > DOWNLOAD_IDLE_TIMEOUT_SECONDS
                ):
                    self._finish_locked(ticket, DownloadState.FAILED, error_code="idle_timeout")
                    continue
            self._prune_locked(now)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _current_session(self) -> PairedSession | None:
        try:
            return self._session_provider()
        except Exception:
            return None

    def _validate_id(self, download_id: object) -> str:
        try:
            return validate_download_id(download_id)
        except DownloadValidationError as exc:
            raise DownloadError(exc.code, exc.message, http_status=404) from exc

    def _get_bound_locked(
        self, session: PairedSession, download_id: str, now: float
    ) -> DownloadTicket:
        self._prune_locked(now)
        ticket = self._tickets.get(download_id)
        if ticket is None:
            raise DownloadError(
                "download_not_found", "The download ticket does not exist.", http_status=404
            )
        if not hmac.compare_digest(ticket.session_id, session.session_id) or not hmac.compare_digest(
            ticket.device_id, session.client_id
        ):
            raise DownloadError(
                "download_forbidden",
                "The download ticket belongs to a different paired device.",
                http_status=403,
            )
        if not self._session_valid_locked(ticket.session_id, now):
            self._finish_locked(ticket, DownloadState.REVOKED, error_code="session_revoked")
            raise DownloadError(
                "session_expired", "The paired session is no longer valid.", http_status=401
            )
        return ticket

    def _session_valid_locked(self, session_id: str, now: float) -> bool:
        session = self._current_session()
        return (
            session is not None
            and session.session_id == session_id
            and not session.is_expired(now)
        )

    def _ensure_source_locked(self, ticket: DownloadTicket) -> None:
        if ticket.grant is None or ticket.grant.closed:
            self._finish_locked(
                ticket, DownloadState.FAILED, error_code="download_source_unavailable"
            )
            raise DownloadError(
                "download_source_unavailable",
                "The download source is no longer available.",
                http_status=503,
            )
        try:
            if _fd_signature(ticket.grant.fd) != _identity_signature(ticket.identity):
                self._source_failed_locked(ticket, "download_source_changed")
        except OSError:
            self._source_failed_locked(ticket, "download_source_unavailable")

    def _source_failed_locked(self, ticket: DownloadTicket, code: str) -> None:
        self._finish_locked(ticket, DownloadState.FAILED, error_code=code)
        raise DownloadError(
            code,
            "The authorized source changed or became unavailable during the download.",
            http_status=422 if code == "download_source_changed" else 503,
        )

    def _terminal_error(self, ticket: DownloadTicket) -> DownloadError:
        if ticket.state == DownloadState.EXPIRED:
            return DownloadError(
                "download_expired", "The download ticket has expired.", http_status=410
            )
        if ticket.state == DownloadState.REVOKED:
            return DownloadError(
                "download_revoked", "The download ticket has been revoked.", http_status=403
            )
        return DownloadError(
            "invalid_download_state",
            "The download is not in a state that permits this operation.",
            http_status=409,
            details={"state": ticket.state.value, "terminal_reason": ticket.error_code},
        )

    def _finish_locked(
        self,
        ticket: DownloadTicket,
        state: DownloadState,
        *,
        error_code: str | None = None,
    ) -> None:
        ticket.state = state
        ticket.error_code = error_code
        ticket.updated_at = self._clock()
        grant = ticket.grant
        ticket.grant = None
        if grant is not None:
            grant.close()
        self._emit_locked("bridge_download_completed", ticket)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - TERMINAL_RETENTION_SECONDS
        stale = [
            download_id
            for download_id, ticket in self._tickets.items()
            if ticket.state in TERMINAL_DOWNLOAD_STATES and ticket.updated_at < cutoff
        ]
        for download_id in stale:
            ticket = self._tickets.pop(download_id, None)
            if ticket is not None and ticket.grant is not None:
                ticket.grant.close()
                ticket.grant = None
        if len(self._tickets) >= MAX_DOWNLOAD_REGISTRY:
            terminal = sorted(
                (
                    ticket
                    for ticket in self._tickets.values()
                    if ticket.state in TERMINAL_DOWNLOAD_STATES
                ),
                key=lambda item: item.updated_at,
            )
            while terminal and len(self._tickets) >= MAX_DOWNLOAD_REGISTRY:
                oldest = terminal.pop(0)
                removed = self._tickets.pop(oldest.download_id, None)
                if removed is not None and removed.grant is not None:
                    removed.grant.close()
                    removed.grant = None

    def _emit_locked(self, event: str, ticket: DownloadTicket) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, public_download(ticket))
        except Exception:
            pass


__all__ = [
    "ACTIVE_DOWNLOAD_STATES",
    "TERMINAL_DOWNLOAD_STATES",
    "DownloadChunk",
    "DownloadError",
    "DownloadManager",
    "DownloadState",
    "DownloadTicket",
    "effective_expires_at",
    "public_download",
]
