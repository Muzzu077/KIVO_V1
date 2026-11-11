"""Authenticated upload state machine, chunk receiving, and final acceptance.

The manager is the single owner of upload transfer state.  HTTP routes
authenticate a request, call :meth:`commit_request` to consume its replay
window slot, and then invoke one state-machine method.  Desktop decisions
arrive through the protected local IPC socket and never through the LAN API.

Storage is reserved only after Gate A (``allow_receive``).  A rejected offer
therefore consumes no quarantine capacity.  Gate B (``accept_file``) is the
only path to Downloads publication.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from kivo.bridge.models import PairedSession
from kivo.bridge.quarantine import (
    PublishedFile,
    QuarantineError,
    QuarantineHandle,
    QuarantineManager,
    Reservation,
)
from kivo.bridge.transfer_crypto import (
    TRANSFER_PROTOCOL_VERSION,
    build_canonical_transfer_request,
    decode_nonce_b64,
    verify_transfer_request_signature,
)
from kivo.bridge.transfer_validation import (
    FINAL_ACCEPTANCE_TTL_SECONDS,
    MAX_ACTIVE_TRANSFERS_PER_SESSION,
    MAX_CONCURRENT_TRANSFERS,
    MAX_RETAINED_TRANSFERS_PER_SESSION,
    MAX_TRANSFER_REGISTRY,
    MAX_UPLOAD_BYTES,
    TERMINAL_RETENTION_SECONDS,
    TRANSFER_APPROVAL_TTL_SECONDS,
    TRANSFER_CHUNK_BYTES,
    TRANSFER_REPLAY_NONCE_LIMIT,
    TRANSFER_REPLAY_WINDOW,
    TRANSFER_STALLED_TIMEOUT_SECONDS,
    TRANSFER_TIMESTAMP_TOLERANCE_SECONDS,
    UploadValidationError,
    validate_declared_size,
    validate_decision,
    validate_display_filename,
    validate_mime_hint,
    validate_sha256_bytes,
    validate_transfer_id,
)

MAX_AUTH_TOKEN_CHARS = 256
MAX_SESSION_ID_CHARS = 64
SESSION_ID_PATTERN = re.compile(r"^sess_[A-Za-z0-9_.-]{1,63}$")


class UploadState(str, Enum):
    CREATED = "created"
    AWAITING_DESKTOP_APPROVAL = "awaiting_desktop_approval"
    RECEIVING = "receiving"
    VERIFYING = "verifying"
    AWAITING_FINAL_ACCEPTANCE = "awaiting_final_acceptance"
    COMMITTED = "committed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    DISCARDED = "discarded"


TERMINAL_STATES = frozenset(
    {
        UploadState.COMMITTED,
        UploadState.REJECTED,
        UploadState.CANCELLED,
        UploadState.EXPIRED,
        UploadState.FAILED,
        UploadState.DISCARDED,
    }
)
ACTIVE_RECEIVING_STATES = frozenset({UploadState.RECEIVING, UploadState.VERIFYING})


class TransferError(Exception):
    """A structured transfer refusal with an HTTP status suggestion."""

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


@dataclass(frozen=True)
class TransferAuth:
    session: PairedSession
    sequence: int
    nonce: bytes
    timestamp: float
    content_length: int = 0
    content_sha256: bytes = b""
    canonical_request: bytes = b""


class _ReplayWindow:
    """Bounded sliding sequence window plus nonce uniqueness.

    HTTP requests for one session are allowed to complete out of order (for
    example, two chunks in flight).  A sequence is accepted if it is not older
    than ``highest - window`` and has not been used before.  The window and
    nonce set are bounded, and all mutation happens under the manager lock.
    """

    def __init__(
        self,
        window: int = TRANSFER_REPLAY_WINDOW,
        nonce_limit: int = TRANSFER_REPLAY_NONCE_LIMIT,
    ):
        self._window = int(window)
        self._nonce_limit = int(nonce_limit)
        self.highest = 0
        self.seen_sequences: set[int] = set()
        self.seen_nonces: dict[bytes, None] = {}

    def check(self, sequence: int, nonce: bytes) -> str | None:
        if sequence <= self.highest - self._window:
            return "replay_sequence_invalid"
        if sequence in self.seen_sequences:
            return "replay_sequence_invalid"
        if nonce in self.seen_nonces:
            return "replay_nonce_detected"
        return None

    def commit(self, sequence: int, nonce: bytes) -> None:
        self.seen_sequences.add(sequence)
        self.seen_nonces[nonce] = None
        if sequence > self.highest:
            self.highest = sequence
        for value in tuple(self.seen_sequences):
            if value <= self.highest - self._window:
                self.seen_sequences.discard(value)
        while len(self.seen_nonces) > self._nonce_limit:
            oldest = next(iter(self.seen_nonces))
            del self.seen_nonces[oldest]


@dataclass
class UploadTransfer:
    transfer_id: str
    session_id: str
    device_name: str
    filename: str
    size: int
    sha256: bytes
    mime: str | None
    state: UploadState = UploadState.CREATED
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    received_bytes: int = 0
    next_chunk_index: int = 0
    last_activity: float = 0.0
    quarantine: QuarantineHandle | None = None
    reservation: Reservation | None = None
    error_code: str | None = None
    published_filename: str | None = None
    incremental_hasher: Any = field(default_factory=hashlib.sha256, repr=False)

    @property
    def total_chunks(self) -> int:
        if self.size == 0:
            return 0
        return (self.size + TRANSFER_CHUNK_BYTES - 1) // TRANSFER_CHUNK_BYTES

    @property
    def total_bytes(self) -> int:
        return self.size


def _progress_percent(transfer: UploadTransfer) -> int:
    if transfer.size == 0:
        return 100 if transfer.state in (
            UploadState.AWAITING_FINAL_ACCEPTANCE,
            UploadState.COMMITTED,
        ) else 0
    if transfer.received_bytes >= transfer.size:
        return 100
    return int((transfer.received_bytes * 100) // transfer.size)


def public_transfer(transfer: UploadTransfer) -> dict[str, Any]:
    """Return only the fields safe for IPC/event delivery."""
    return {
        "transfer_id": transfer.transfer_id,
        "filename": transfer.filename,
        "size": transfer.size,
        "received_bytes": transfer.received_bytes,
        "progress_percent": _progress_percent(transfer),
        "state": transfer.state.value,
        "device_name": transfer.device_name,
        "mime": transfer.mime,
        "sha256": transfer.sha256.hex(),
        "error_code": transfer.error_code,
        "published_filename": transfer.published_filename,
        "chunk_size": TRANSFER_CHUNK_BYTES,
        "next_chunk_index": transfer.next_chunk_index,
        "created_at": round(transfer.created_at, 3),
        "updated_at": round(transfer.updated_at, 3),
        "expires_at": round(transfer.expires_at, 3),
    }


class TransferManager:
    """Single owner of authenticated upload transfers and quarantine state."""

    def __init__(
        self,
        *,
        session_provider: Callable[[], PairedSession | None],
        quarantine: QuarantineManager,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
        cleanup_interval: float = 5.0,
    ):
        self._session_provider = session_provider
        self._quarantine = quarantine
        self._on_event = on_event
        self._clock = clock
        self._cleanup_interval = max(0.1, float(cleanup_interval))
        self._lock = threading.RLock()
        self._transfers: dict[str, UploadTransfer] = {}
        self._replay_windows: dict[str, _ReplayWindow] = {}
        self._started = False
        self._closing = False
        self._closed = False
        self._stop_event = threading.Event()
        self._cleanup_thread: threading.Thread | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise TransferError(
                    "transfer_unavailable", "The transfer manager is closed.", http_status=503
                )
            if self._started:
                return
            self._quarantine.start()
            self._closing = False
            self._started = True
            self._stop_event.clear()
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name="kivo-transfer-cleanup",
                daemon=True,
            )
            self._cleanup_thread.start()

    def stop(self, reason: str = "bridge_stopped") -> None:
        """Cancel active transfers and stop the cleanup thread (bounded)."""
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
            try:
                self._quarantine.cleanup_abandoned()
            except Exception:
                pass
            self._quarantine.close()
            self._replay_windows.clear()
            self._transfers.clear()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._started and not self._closed

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(self._cleanup_interval):
            try:
                self.cleanup_expired()
            except Exception:
                # Cleanup is best effort; the next tick retries.
                continue

    # ── Authentication and replay ──────────────────────────────────────────

    def authenticate_request(
        self,
        *,
        bearer_token: str | None,
        protocol_version: str | None,
        session_id: str | None,
        method: str,
        endpoint: str,
        sequence_value: str | int | None,
        nonce_b64: str | None,
        timestamp_value: str | float | None,
        content_length: int,
        content_sha256_hex: str | None,
    ) -> TransferAuth:
        """Validate identity, freshness, and the ``K_up`` request MAC.

        Replay state is not mutated here.  The route calls
        :meth:`commit_request` after the actual body digest has been checked.
        """
        if protocol_version != TRANSFER_PROTOCOL_VERSION:
            raise TransferError(
                "unsupported_protocol_version",
                "The transfer protocol version is not supported.",
                http_status=400,
            )
        if not bearer_token or len(bearer_token) > MAX_AUTH_TOKEN_CHARS:
            raise TransferError("missing_authorization", "A session bearer token is required.", http_status=401)
        if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
            raise TransferError("invalid_session", "The session identifier is malformed.", http_status=401)

        try:
            sequence = int(sequence_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise TransferError("invalid_sequence", "The request sequence is invalid.", http_status=400)
        if isinstance(sequence_value, bool) or sequence < 1 or sequence > 2**63 - 1:
            raise TransferError("invalid_sequence", "The request sequence is out of range.", http_status=400)

        try:
            nonce = decode_nonce_b64(nonce_b64)
        except ValueError as exc:
            raise TransferError("invalid_nonce", str(exc), http_status=400) from exc

        try:
            timestamp = float(timestamp_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise TransferError("invalid_timestamp", "The request timestamp is invalid.", http_status=400)
        if not math.isfinite(timestamp) or not 0.0 <= timestamp <= 4102444800.0:
            raise TransferError("invalid_timestamp", "The request timestamp is out of range.", http_status=400)
        now = self._clock()
        if abs(now - timestamp) > TRANSFER_TIMESTAMP_TOLERANCE_SECONDS:
            raise TransferError(
                "timestamp_out_of_bounds",
                "The request timestamp is outside the accepted freshness window.",
                http_status=401,
            )

        if not isinstance(content_length, int) or isinstance(content_length, bool):
            raise TransferError("invalid_content_length", "Content length must be an integer.", http_status=400)
        if not 0 <= content_length <= 2**63 - 1:
            raise TransferError("invalid_content_length", "Content length is out of range.", http_status=400)
        if not isinstance(content_sha256_hex, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", content_sha256_hex
        ):
            raise TransferError(
                "invalid_content_hash",
                "The declared body SHA-256 must be exactly 64 hexadecimal characters.",
                http_status=400,
            )
        content_sha256 = bytes.fromhex(content_sha256_hex)

        with self._lock:
            session = self._session_provider()
            if session is None:
                raise TransferError("session_invalid", "No active paired session exists.", http_status=401)
            if not hmac.compare_digest(str(session.bearer_token), str(bearer_token)):
                raise TransferError("session_invalid", "The session bearer token is invalid.", http_status=401)
            if not hmac.compare_digest(str(session.session_id), str(session_id)):
                raise TransferError("session_invalid", "The session identifier does not match.", http_status=401)
            if session.is_expired(now):
                raise TransferError("session_expired", "The paired session has expired.", http_status=401)

            canonical = build_canonical_transfer_request(
                version=TRANSFER_PROTOCOL_VERSION,
                session_id=session_id,
                method=method.upper(),
                endpoint=endpoint,
                sequence=sequence,
                nonce=nonce,
                timestamp=timestamp,
                content_length=content_length,
                content_sha256=content_sha256,
            )
            return TransferAuth(
                session=session,
                sequence=sequence,
                nonce=nonce,
                timestamp=timestamp,
                content_length=content_length,
                content_sha256=content_sha256,
                canonical_request=canonical,
            )

    def verify_request_signature(
        self, auth: TransferAuth, canonical_request: bytes, signature: bytes
    ) -> bool:
        return verify_transfer_request_signature(
            auth.session.upstream_key, canonical_request, signature
        )

    def build_canonical_request(
        self,
        *,
        session_id: str,
        method: str,
        endpoint: str,
        sequence: int,
        nonce: bytes,
        timestamp: float,
        content_length: int,
        content_sha256: bytes,
    ) -> bytes:
        return build_canonical_transfer_request(
            version=TRANSFER_PROTOCOL_VERSION,
            session_id=session_id,
            method=method.upper(),
            endpoint=endpoint,
            sequence=sequence,
            nonce=nonce,
            timestamp=timestamp,
            content_length=content_length,
            content_sha256=content_sha256,
        )

    def commit_request(self, auth: TransferAuth) -> None:
        """Consume one request sequence/nonce after all body checks pass."""
        with self._lock:
            session = self._session_provider()
            if (
                session is None
                or session.session_id != auth.session.session_id
                or session.is_expired(self._clock())
            ):
                raise TransferError("session_expired", "The paired session is no longer valid.", http_status=401)
            window = self._replay_windows.get(auth.session.session_id)
            if window is None:
                if len(self._replay_windows) >= 8:
                    oldest = next(iter(self._replay_windows))
                    self._replay_windows.pop(oldest, None)
                window = _ReplayWindow()
                self._replay_windows[auth.session.session_id] = window
            error = window.check(auth.sequence, auth.nonce)
            if error == "replay_nonce_detected":
                raise TransferError(
                    "replay_nonce_detected",
                    "This request nonce has already been used.",
                    http_status=409,
                )
            if error is not None:
                raise TransferError(
                    "replay_sequence_invalid",
                    "This request sequence is outside the accepted replay window.",
                    http_status=409,
                )
            window.commit(auth.sequence, auth.nonce)

    # ── Metadata and initiation ────────────────────────────────────────────

    def create_upload(
        self,
        session: PairedSession,
        *,
        filename: object,
        size: object,
        sha256_hex: object,
        mime: object = None,
    ) -> UploadTransfer:
        try:
            safe_name = validate_display_filename(filename)
            declared_size = validate_declared_size(size)
            digest = validate_sha256_bytes(
                bytes.fromhex(sha256_hex) if isinstance(sha256_hex, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha256_hex) else b""
            )
            mime_hint = validate_mime_hint(mime)
        except (UploadValidationError, ValueError) as exc:
            if isinstance(exc, UploadValidationError):
                raise TransferError(exc.code, exc.message, http_status=400) from exc
            raise TransferError("invalid_sha256", "sha256 must be exactly 64 hexadecimal characters.", http_status=400) from exc

        now = self._clock()
        with self._lock:
            if not self._started or self._closing or self._closed:
                raise TransferError("transfer_unavailable", "File transfer is not available.", http_status=503)
            session_now = self._session_provider()
            if (
                session_now is None
                or session_now.session_id != session.session_id
                or session_now.is_expired(now)
            ):
                raise TransferError("session_expired", "The paired session is no longer valid.", http_status=401)

            self._prune_locked(now)
            session_transfers = [
                item
                for item in self._transfers.values()
                if item.session_id == session.session_id and item.state not in TERMINAL_STATES
            ]
            if len(session_transfers) >= MAX_ACTIVE_TRANSFERS_PER_SESSION:
                raise TransferError(
                    "session_transfer_limit",
                    "This paired device already has the maximum number of active transfers.",
                    http_status=429,
                )
            if len(self._transfers) >= MAX_TRANSFER_REGISTRY:
                raise TransferError(
                    "transfer_registry_full",
                    "The transfer registry is currently full.",
                    http_status=429,
                )
            transfer_id = "tr_" + secrets.token_hex(12)
            while transfer_id in self._transfers:
                transfer_id = "tr_" + secrets.token_hex(12)
            transfer = UploadTransfer(
                transfer_id=transfer_id,
                session_id=session.session_id,
                device_name=session.device_name,
                filename=safe_name,
                size=declared_size,
                sha256=digest,
                mime=mime_hint,
                state=UploadState.AWAITING_DESKTOP_APPROVAL,
                created_at=now,
                updated_at=now,
                expires_at=now + TRANSFER_APPROVAL_TTL_SECONDS,
                last_activity=now,
            )
            self._transfers[transfer_id] = transfer
            self._emit_locked("bridge_transfer_requested", transfer)
            return transfer

    def get_transfer(self, transfer_id: str, *, session_id: str | None = None) -> UploadTransfer:
        try:
            safe_id = validate_transfer_id(transfer_id)
        except UploadValidationError as exc:
            raise TransferError(exc.code, exc.message, http_status=404) from exc
        with self._lock:
            self._prune_locked(self._clock())
            transfer = self._transfers.get(safe_id)
            if transfer is None:
                raise TransferError("transfer_not_found", "The transfer does not exist.", http_status=404)
            if session_id is not None and transfer.session_id != session_id:
                raise TransferError(
                    "transfer_forbidden",
                    "The transfer belongs to a different paired session.",
                    http_status=403,
                )
            return transfer

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            self._prune_locked(self._clock())
            return [
                public_transfer(item)
                for item in sorted(self._transfers.values(), key=lambda item: item.created_at)
            ]

    # ── Desktop decisions ──────────────────────────────────────────────────

    def resolve(self, transfer_id: str, decision: object) -> UploadTransfer:
        try:
            safe_decision = validate_decision(decision)
        except UploadValidationError as exc:
            raise TransferError(exc.code, exc.message, http_status=400) from exc
        transfer = self.get_transfer(transfer_id)
        with self._lock:
            transfer = self._transfers.get(transfer.transfer_id)
            if transfer is None:
                raise TransferError("transfer_not_found", "The transfer does not exist.", http_status=404)
            if safe_decision == "allow_receive":
                return self._allow_receive_locked(transfer)
            if safe_decision == "reject_receive":
                if transfer.state != UploadState.AWAITING_DESKTOP_APPROVAL:
                    raise TransferError(
                        "invalid_transfer_state",
                        "Only an offer awaiting desktop approval can be rejected.",
                        http_status=409,
                    )
                self._finish_locked(transfer, UploadState.REJECTED)
                return transfer
            if safe_decision == "accept_file":
                return self._accept_file_locked(transfer)
            if safe_decision == "discard_file":
                if transfer.state != UploadState.AWAITING_FINAL_ACCEPTANCE:
                    raise TransferError(
                        "invalid_transfer_state",
                        "Only a verified file awaiting final acceptance can be discarded.",
                        http_status=409,
                    )
                self._finish_locked(transfer, UploadState.DISCARDED)
                return transfer
            raise TransferError("invalid_decision", "Unsupported desktop decision.", http_status=400)

    def cancel(self, transfer_id: str) -> UploadTransfer:
        transfer = self.get_transfer(transfer_id)
        with self._lock:
            transfer = self._transfers.get(transfer.transfer_id)
            if transfer is None:
                raise TransferError("transfer_not_found", "The transfer does not exist.", http_status=404)
            if transfer.state in TERMINAL_STATES:
                raise TransferError(
                    "invalid_transfer_state",
                    "The transfer is already terminal.",
                    http_status=409,
                )
            self._finish_locked(transfer, UploadState.CANCELLED)
            return transfer

    def _allow_receive_locked(self, transfer: UploadTransfer) -> UploadTransfer:
        if transfer.state != UploadState.AWAITING_DESKTOP_APPROVAL:
            raise TransferError(
                "invalid_transfer_state",
                "Only an offer awaiting desktop approval can be allowed.",
                http_status=409,
            )
        if self._clock() >= transfer.expires_at:
            self._finish_locked(transfer, UploadState.EXPIRED)
            raise TransferError("approval_expired", "The receive offer has expired.", http_status=410)

        receiving = sum(
            1 for item in self._transfers.values() if item.state in ACTIVE_RECEIVING_STATES
        )
        if receiving >= MAX_CONCURRENT_TRANSFERS:
            raise TransferError(
                "concurrent_transfer_limit",
                "The maximum number of concurrent transfers is already active.",
                http_status=429,
            )
        tier = self._quarantine.tier_for_size(transfer.size)
        reservation: Reservation | None = None
        handle: QuarantineHandle | None = None
        try:
            reservation = self._quarantine.reserve(transfer.size, tier)
            handle = self._quarantine.create_file(reservation)
        except QuarantineError as exc:
            if reservation is not None:
                self._quarantine.release(reservation)
            self._finish_locked(transfer, UploadState.FAILED, error_code=exc.code)
            raise self._map_quarantine_error(exc) from exc

        transfer.reservation = reservation
        transfer.quarantine = handle
        transfer.state = UploadState.RECEIVING
        transfer.updated_at = self._clock()
        transfer.last_activity = transfer.updated_at
        transfer.error_code = None
        self._emit_locked("bridge_transfer_progress", transfer)
        if transfer.size == 0:
            self._verify_locked(transfer)
        return transfer

    def _accept_file_locked(self, transfer: UploadTransfer) -> UploadTransfer:
        if transfer.state != UploadState.AWAITING_FINAL_ACCEPTANCE:
            raise TransferError(
                "invalid_transfer_state",
                "Only a verified file awaiting final acceptance can be published.",
                http_status=409,
            )
        if self._clock() >= transfer.expires_at:
            self._finish_locked(transfer, UploadState.EXPIRED)
            raise TransferError(
                "final_acceptance_expired",
                "The verified file expired before final acceptance.",
                http_status=410,
            )
        if transfer.quarantine is None:
            self._finish_locked(transfer, UploadState.FAILED, error_code="quarantine_missing")
            raise TransferError("quarantine_missing", "The verified file is unavailable.", http_status=500)
        try:
            published: PublishedFile = self._quarantine.publish(
                transfer.quarantine, transfer.filename, expected_sha256=transfer.sha256
            )
        except QuarantineError as exc:
            transfer.error_code = exc.code
            transfer.updated_at = self._clock()
            if exc.code in ("file_modified", "file_replaced", "integrity_failed", "unsafe_permissions"):
                self._finish_locked(transfer, UploadState.FAILED, error_code=exc.code)
            raise self._map_quarantine_error(exc) from exc
        transfer.published_filename = published.name
        self._finish_locked(transfer, UploadState.COMMITTED, error_code=None)
        return transfer

    # ── Chunk ingestion ────────────────────────────────────────────────────

    def accept_chunk(
        self,
        session: PairedSession,
        transfer_id: str,
        chunk_index: int,
        data: bytes,
        declared_sha256: bytes,
    ) -> UploadTransfer:
        try:
            safe_id = validate_transfer_id(transfer_id)
        except UploadValidationError as exc:
            raise TransferError(exc.code, exc.message, http_status=404) from exc
        if not isinstance(chunk_index, int) or isinstance(chunk_index, bool) or chunk_index < 0:
            raise TransferError("invalid_chunk_index", "The chunk index is invalid.", http_status=400)
        if not isinstance(declared_sha256, (bytes, bytearray)) or len(declared_sha256) != 32:
            raise TransferError("invalid_chunk_hash", "The chunk SHA-256 is invalid.", http_status=400)

        with self._lock:
            transfer = self._transfers.get(safe_id)
            if transfer is None:
                raise TransferError("transfer_not_found", "The transfer does not exist.", http_status=404)
            if transfer.session_id != session.session_id:
                raise TransferError(
                    "transfer_forbidden",
                    "The transfer belongs to a different paired session.",
                    http_status=403,
                )
            if transfer.state != UploadState.RECEIVING:
                raise TransferError(
                    "invalid_transfer_state",
                    "The transfer is not currently receiving chunks.",
                    http_status=409,
                    details={"state": transfer.state.value},
                )
            total_chunks = transfer.total_chunks
            if chunk_index >= total_chunks:
                raise TransferError(
                    "invalid_chunk_index",
                    "The chunk index is outside the declared file.",
                    http_status=400,
                    details={"total_chunks": total_chunks},
                )
            offset = chunk_index * TRANSFER_CHUNK_BYTES
            expected_length = min(TRANSFER_CHUNK_BYTES, transfer.size - offset)
            if len(data) != expected_length:
                code = (
                    "final_chunk_length_mismatch"
                    if chunk_index == total_chunks - 1
                    else "chunk_length_mismatch"
                )
                raise TransferError(
                    code,
                    "The chunk length does not match the declared file geometry.",
                    http_status=400,
                    details={"expected_length": expected_length, "received_length": len(data)},
                )
            if chunk_index < transfer.next_chunk_index:
                existing = self._quarantine.chunk_digest(transfer.quarantine, offset, expected_length)
                if hmac.compare_digest(existing, bytes(declared_sha256)):
                    return transfer
                raise TransferError(
                    "chunk_conflict",
                    "The duplicate chunk does not match the already committed contents.",
                    http_status=409,
                )
            if chunk_index > transfer.next_chunk_index:
                raise TransferError(
                    "chunk_out_of_order",
                    "Chunks must be committed in order.",
                    http_status=409,
                    details={
                        "expected_chunk_index": transfer.next_chunk_index,
                        "received_bytes": transfer.received_bytes,
                    },
                )
            actual = hashlib.sha256(data).digest()
            if not hmac.compare_digest(actual, bytes(declared_sha256)):
                raise TransferError(
                    "chunk_hash_mismatch",
                    "The chunk SHA-256 does not match the declared digest.",
                    http_status=422,
                )
            try:
                new_offset = self._quarantine.append_chunk(
                    transfer.quarantine,
                    data,
                    expected_offset=transfer.received_bytes,
                    expected_total_size=transfer.size,
                )
            except QuarantineError as exc:
                if exc.code in (
                    "insufficient_storage",
                    "file_modified",
                    "file_replaced",
                    "write_failed",
                    "unsafe_permissions",
                ):
                    self._finish_locked(transfer, UploadState.FAILED, error_code=exc.code)
                raise self._map_quarantine_error(exc) from exc

            transfer.received_bytes = new_offset
            transfer.next_chunk_index = chunk_index + 1
            transfer.last_activity = self._clock()
            transfer.updated_at = transfer.last_activity
            transfer.incremental_hasher.update(data)
            if transfer.received_bytes == transfer.size:
                self._verify_locked(transfer)
            else:
                self._emit_locked("bridge_transfer_progress", transfer)
            return transfer

    def _verify_locked(self, transfer: UploadTransfer) -> None:
        transfer.state = UploadState.VERIFYING
        transfer.updated_at = self._clock()
        self._emit_locked("bridge_transfer_progress", transfer)
        try:
            self._quarantine.seal(
                transfer.quarantine,
                expected_size=transfer.size,
                expected_sha256=transfer.sha256,
                incremental_sha256=transfer.incremental_hasher.digest(),
            )
        except QuarantineError as exc:
            self._finish_locked(transfer, UploadState.FAILED, error_code=exc.code)
            return
        transfer.state = UploadState.AWAITING_FINAL_ACCEPTANCE
        transfer.updated_at = self._clock()
        transfer.expires_at = transfer.updated_at + FINAL_ACCEPTANCE_TTL_SECONDS
        transfer.error_code = None
        self._emit_locked("bridge_transfer_progress", transfer)

    # ── Cleanup ────────────────────────────────────────────────────────────

    def cleanup_expired(self) -> None:
        now = self._clock()
        with self._lock:
            for transfer in list(self._transfers.values()):
                if transfer.state in TERMINAL_STATES:
                    continue
                if not self._session_valid_locked(transfer.session_id):
                    self._finish_locked(transfer, UploadState.CANCELLED, error_code="session_expired")
                    continue
                if (
                    transfer.state == UploadState.AWAITING_DESKTOP_APPROVAL
                    and now >= transfer.expires_at
                ):
                    self._finish_locked(transfer, UploadState.EXPIRED, error_code="approval_expired")
                    continue
                if (
                    transfer.state == UploadState.RECEIVING
                    and now - transfer.last_activity > TRANSFER_STALLED_TIMEOUT_SECONDS
                ):
                    self._finish_locked(transfer, UploadState.FAILED, error_code="stalled_timeout")
                    continue
                if (
                    transfer.state == UploadState.AWAITING_FINAL_ACCEPTANCE
                    and now >= transfer.expires_at
                ):
                    self._finish_locked(transfer, UploadState.EXPIRED, error_code="final_acceptance_expired")
                    continue
            self._prune_locked(now)

    def cancel_session(self, session_id: str, reason: str = "session_revoked") -> int:
        cancelled = 0
        with self._lock:
            for transfer in list(self._transfers.values()):
                if transfer.session_id == session_id and transfer.state not in TERMINAL_STATES:
                    self._finish_locked(transfer, UploadState.CANCELLED, error_code=reason)
                    cancelled += 1
            self._replay_windows.pop(session_id, None)
        return cancelled

    def cancel_all(self, reason: str = "bridge_stopped") -> int:
        with self._lock:
            return self._cancel_all_locked(reason)

    def _cancel_all_locked(self, reason: str) -> int:
        cancelled = 0
        for transfer in list(self._transfers.values()):
            if transfer.state not in TERMINAL_STATES:
                self._finish_locked(transfer, UploadState.CANCELLED, error_code=reason)
                cancelled += 1
        self._replay_windows.clear()
        return cancelled

    def _session_valid_locked(self, session_id: str) -> bool:
        try:
            session = self._session_provider()
        except Exception:
            return False
        return (
            session is not None
            and session.session_id == session_id
            and not session.is_expired(self._clock())
        )

    def _finish_locked(
        self,
        transfer: UploadTransfer,
        state: UploadState,
        *,
        error_code: str | None = None,
    ) -> None:
        transfer.state = state
        transfer.error_code = error_code
        transfer.updated_at = self._clock()
        self._cleanup_transfer_locked(transfer)
        self._emit_locked("bridge_transfer_completed", transfer)

    def _cleanup_transfer_locked(self, transfer: UploadTransfer) -> None:
        if transfer.quarantine is not None:
            try:
                self._quarantine.discard(transfer.quarantine)
            except Exception:
                pass
            transfer.quarantine = None
        if transfer.reservation is not None:
            try:
                self._quarantine.release(transfer.reservation)
            except Exception:
                pass
            transfer.reservation = None

    def _prune_locked(self, now: float) -> None:
        cutoff = now - TERMINAL_RETENTION_SECONDS
        stale = [
            transfer_id
            for transfer_id, transfer in self._transfers.items()
            if transfer.state in TERMINAL_STATES and transfer.updated_at < cutoff
        ]
        for transfer_id in stale:
            self._transfers.pop(transfer_id, None)
        if len(self._transfers) >= MAX_TRANSFER_REGISTRY:
            terminal = sorted(
                (
                    transfer
                    for transfer in self._transfers.values()
                    if transfer.state in TERMINAL_STATES
                ),
                key=lambda item: item.updated_at,
            )
            while terminal and len(self._transfers) >= MAX_TRANSFER_REGISTRY:
                oldest = terminal.pop(0)
                self._transfers.pop(oldest.transfer_id, None)

    def _emit_locked(self, event: str, transfer: UploadTransfer) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, public_transfer(transfer))
        except Exception:
            # Event delivery must never corrupt transfer state.
            pass

    @staticmethod
    def _map_quarantine_error(exc: QuarantineError) -> TransferError:
        status = 500
        if exc.code == "insufficient_storage":
            status = 507
        elif exc.code == "aggregate_capacity_exceeded":
            status = 507
        elif exc.code in (
            "quarantine_unavailable",
            "quarantine_root_replaced",
            "quarantine_root_unsafe",
            "mount_identity_unavailable",
            "no_overwrite_unsupported",
            "downloads_unavailable",
            "read_only_filesystem",
        ):
            status = 503
        elif exc.code in ("permission_denied",):
            status = 403
        elif exc.code in (
            "file_modified",
            "file_replaced",
            "integrity_failed",
            "unexpected_eof",
            "chunk_too_large",
            "chunk_length_mismatch",
            "invalid_chunk",
        ):
            status = 422
        elif exc.code in ("invalid_transfer_state",):
            status = 409
        return TransferError(exc.code, exc.message, http_status=status)


__all__ = [
    "UploadState",
    "UploadTransfer",
    "TransferAuth",
    "TransferError",
    "TransferManager",
    "public_transfer",
]
