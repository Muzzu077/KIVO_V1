"""State machine and session manager for Phone Bridge pairing."""
from __future__ import annotations

import base64
import os
import threading
import time
import uuid
from typing import Any, Optional

from kivo.bridge.crypto import (
    build_canonical_context,
    build_canonical_poll_request,
    build_canonical_poll_response,
    compute_credential_sha256,
    compute_poll_response_signature,
    derive_poll_key,
    derive_sas_code,
    derive_session_keys,
    verify_client_proof,
    verify_poll_signature,
)
from kivo.bridge.models import (
    ALGORITHM_ID,
    APPROVAL_RECOVERY_WINDOW,
    IP_LOCKOUT_SECONDS,
    MAX_FAILED_ATTEMPTS_PER_IP,
    MAX_FAILED_PROOFS_PER_IP,
    MAX_TOTAL_FAILED_PROOFS_PER_OFFER,
    MAX_TRACKED_RATE_LIMIT_IPS,
    PAIRING_APPROVAL_TTL_SECONDS,
    PAIRING_OFFER_TTL_SECONDS,
    PAIRING_PROTOCOL_VERSION,
    RATE_LIMIT_WINDOW_SECONDS,
    SESSION_TTL_SECONDS,
    BridgeState,
    PairedSession,
    PairingOffer,
    PairingRequest,
)
from kivo.bridge.qr import build_qr_uri, render_qr_data_url


class PairingManager:
    """Manages ephemeral pairing offers, SAS verification, and session state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = BridgeState.IDLE
        self._current_offer: PairingOffer | None = None
        self._pending_request: PairingRequest | None = None
        self._active_session: PairedSession | None = None
        self._consumed_session_ids: set[str] = set()
        self._poll_keys: dict[str, bytes] = {}
        self._poll_sequences: dict[str, int] = {}
        self._poll_nonces: dict[str, set[bytes]] = {}
        self._cached_session_keys: dict[str, tuple[bytes, bytes, str]] = {}
        self._approval_time: dict[str, float] = {}
        self._ip_lockouts: dict[str, float] = {}
        self._ip_failed_proofs: dict[str, int] = {}
        self._total_failed_proofs: int = 0
        self._failed_attempts: dict[str, list[float]] = {}
        self._approval_event = threading.Event()

    @property
    def state(self) -> BridgeState:
        with self._lock:
            self._cleanup_expired_locked()
            return self._state

    @property
    def current_offer(self) -> PairingOffer | None:
        with self._lock:
            self._cleanup_expired_locked()
            return self._current_offer

    @property
    def pending_request(self) -> PairingRequest | None:
        with self._lock:
            self._cleanup_expired_locked()
            return self._pending_request

    @property
    def active_session(self) -> PairedSession | None:
        with self._lock:
            self._cleanup_expired_locked()
            return self._active_session

    def create_offer(
        self, lan_ip: str, port: int, cert_fp: str
    ) -> PairingOffer:
        """Create a new ephemeral pairing offer with a 5-minute TTL."""
        with self._lock:
            self.reset_locked()

            session_id = f"sess_{uuid.uuid4().hex[:16]}"
            pairing_secret = os.urandom(32)
            server_nonce = os.urandom(32)

            now = time.time()
            expires_at = now + PAIRING_OFFER_TTL_SECONDS

            secret_b64 = base64.urlsafe_b64encode(pairing_secret).decode("ascii").rstrip("=")
            nonce_b64 = base64.urlsafe_b64encode(server_nonce).decode("ascii").rstrip("=")

            qr_uri = build_qr_uri(
                lan_ip=lan_ip,
                port=port,
                cert_fp=cert_fp,
                pairing_secret_b64=secret_b64,
                session_id=session_id,
                server_nonce_b64=nonce_b64,
                expires_at=int(expires_at),
            )
            qr_data_url = render_qr_data_url(qr_uri)

            offer = PairingOffer(
                session_id=session_id,
                lan_ip=lan_ip,
                port=port,
                cert_fingerprint=cert_fp,
                pairing_secret=pairing_secret,
                server_nonce=server_nonce,
                qr_payload=qr_uri,
                qr_data_url=qr_data_url,
                created_at=now,
                expires_at=expires_at,
            )

            self._current_offer = offer
            self._failed_proof_count = 0
            self._state = BridgeState.OFFERING
            self._approval_event.clear()
            return offer

    def handle_client_handshake(
        self,
        session_id: str,
        client_id: str,
        device_name: str,
        client_ip: str,
        client_nonce: bytes,
        client_proof: bytes,
        version: str = PAIRING_PROTOCOL_VERSION,
    ) -> tuple[bool, str | None, str | None]:
        """Validate client proof and atomically consume token if proof is valid.

        Returns:
            (ok: bool, error_code: str | None, sas_code: str | None)
        """
        now = time.time()
        with self._lock:
            self._cleanup_expired_locked()

            # 1. Version validation (does NOT consume token)
            if version != PAIRING_PROTOCOL_VERSION:
                return False, "unsupported_protocol_version", None

            # 2. IP Lockout and Rate limiting check
            if client_ip in self._ip_lockouts:
                if now < self._ip_lockouts[client_ip]:
                    return False, "ip_locked_out", None
                else:
                    self._ip_lockouts.pop(client_ip, None)
                    self._ip_failed_proofs.pop(client_ip, None)

            ip_attempts = [
                t for t in self._failed_attempts.get(client_ip, [])
                if now - t < RATE_LIMIT_WINDOW_SECONDS
            ]
            if len(ip_attempts) >= MAX_FAILED_ATTEMPTS_PER_IP:
                return False, "rate_limited", None

            # 3. Replay check (consumed token is strictly rejected)
            if session_id in self._consumed_session_ids:
                return False, "token_already_consumed", None

            # 4. State and active offer presence
            if self._state != BridgeState.OFFERING or self._current_offer is None:
                self._record_failed_attempt(client_ip, now)
                return False, "no_active_offer", None

            # 5. Offer expiration check
            if self._current_offer.is_expired(now):
                self._state = BridgeState.EXPIRED
                self._consumed_session_ids.add(session_id)
                self._current_offer = None
                self._record_failed_attempt(client_ip, now)
                return False, "offer_expired", None

            # 6. Session ID match check (mismatch does NOT consume token)
            if self._current_offer.session_id != session_id:
                self._record_failed_attempt(client_ip, now)
                return False, "invalid_session_id", None

            # 7. Canonical transcript and proof verification
            context_bytes = build_canonical_context(
                version=PAIRING_PROTOCOL_VERSION,
                session_id=session_id,
                cert_fp=self._current_offer.cert_fingerprint,
                server_nonce=self._current_offer.server_nonce,
                client_nonce=client_nonce,
                client_id=client_id,
                algorithm_id=ALGORITHM_ID,
            )

            valid_proof = verify_client_proof(
                self._current_offer.pairing_secret, context_bytes, client_proof
            )

            if not valid_proof:
                # Invalid proof does NOT destroy offer for legitimate clients,
                # but tracks per-IP failures to lock out offending client.
                self._ip_failed_proofs[client_ip] = self._ip_failed_proofs.get(client_ip, 0) + 1
                self._total_failed_proofs += 1
                self._record_failed_attempt(client_ip, now)

                if self._ip_failed_proofs[client_ip] >= MAX_FAILED_PROOFS_PER_IP:
                    self._ip_lockouts[client_ip] = now + IP_LOCKOUT_SECONDS
                    return False, "ip_locked_out", None

                if self._total_failed_proofs >= MAX_TOTAL_FAILED_PROOFS_PER_OFFER:
                    # Invalidate offer after maximum distributed failures to stop brute force
                    self._state = BridgeState.EXPIRED
                    self._consumed_session_ids.add(session_id)
                    self._current_offer = None
                    return False, "too_many_failed_attempts", None

                return False, "invalid_proof", None

            # 8. VALID PROOF: Atomically consume the token!
            self._consumed_session_ids.add(session_id)
            if len(self._consumed_session_ids) > 100:
                self._consumed_session_ids.pop()

            # Derive K_poll before discarding T_pair
            k_poll = derive_poll_key(
                self._current_offer.pairing_secret, client_nonce, context_bytes
            )
            self._poll_keys[session_id] = k_poll
            self._poll_sequences[session_id] = 0
            self._poll_nonces[session_id] = set()

            # Derive directional session keys and bearer token
            k_up, k_down, token = derive_session_keys(
                self._current_offer.pairing_secret,
                client_nonce,
                context_bytes,
            )
            self._cached_session_keys[session_id] = (k_up, k_down, token)

            # 9. Derive SAS code
            _, sas_formatted = derive_sas_code(
                self._current_offer.pairing_secret,
                self._current_offer.server_nonce,
                context_bytes,
            )

            # 10. Discard original pairing secret T_pair from memory!
            self._current_offer = None

            # 11. Transition atomically to CONFIRMING state
            req = PairingRequest(
                session_id=session_id,
                client_id=client_id,
                device_name=device_name,
                client_ip=client_ip,
                client_nonce=client_nonce,
                sas_code=sas_formatted,
                context_bytes=context_bytes,
                received_at=now,
                expires_at=now + PAIRING_APPROVAL_TTL_SECONDS,
            )
            self._pending_request = req
            self._state = BridgeState.CONFIRMING
            self._approval_event.clear()
            return True, None, sas_formatted

    def wait_for_desktop_approval(self, timeout: float = 60.0) -> bool:
        """Called by the HTTP server to await user approval on desktop."""
        return self._approval_event.wait(timeout=timeout)

    def approve_pairing(self, session_id: str) -> tuple[bool, PairedSession | None]:
        """Desktop user confirms the SAS code match and approves pairing."""
        now = time.time()
        with self._lock:
            self._cleanup_expired_locked()

            if (
                self._state != BridgeState.CONFIRMING
                or self._pending_request is None
            ):
                return False, None

            if self._pending_request.session_id != session_id:
                return False, None

            if self._pending_request.is_expired(now):
                self._state = BridgeState.EXPIRED
                self._consumed_session_ids.add(session_id)
                self._discard_poll_state_locked(session_id)
                return False, None

            cached = self._cached_session_keys.get(session_id)
            if not cached:
                return False, None
            k_up, k_down, token = cached

            session = PairedSession(
                session_id=session_id,
                client_id=self._pending_request.client_id,
                device_name=self._pending_request.device_name,
                client_ip=self._pending_request.client_ip,
                bearer_token=token,
                upstream_key=k_up,
                downstream_key=k_down,
                created_at=now,
                last_active=now,
                expires_at=now + SESSION_TTL_SECONDS,
                is_revoked=False,
            )

            self._active_session = session
            self._state = BridgeState.PAIRED
            self._approval_time[session_id] = now
            self._consumed_session_ids.add(session_id)
            # Session now owns the keys; drop the duplicate pending copies.
            self._cached_session_keys.pop(session_id, None)
            self._pending_request = None
            self._approval_event.set()
            return True, session

    def reject_pairing(self, session_id: str) -> bool:
        """Desktop user explicitly rejects the pairing candidate."""
        with self._lock:
            if self._state == BridgeState.CONFIRMING and self._pending_request:
                if self._pending_request.session_id == session_id:
                    self._state = BridgeState.REJECTED
                    self._pending_request = None
                    self._consumed_session_ids.add(session_id)
                    self._discard_poll_state_locked(session_id)
                    self._current_offer = None  # Permanently consumed
                    self._approval_event.set()
                    return True
            return False

    def revoke_device(self, session_id: str) -> bool:
        """Revoke active session token immediately."""
        with self._lock:
            if self._active_session and self._active_session.session_id == session_id:
                self._active_session = None
                self._state = BridgeState.REVOKED
                self._consumed_session_ids.add(session_id)
                self._discard_poll_state_locked(session_id)
                return True
            return False

    def _discard_poll_state_locked(self, session_id: str) -> None:
        """Best-effort removal of all polling and credential material for a session."""
        self._poll_keys.pop(session_id, None)
        self._poll_sequences.pop(session_id, None)
        self._poll_nonces.pop(session_id, None)
        self._cached_session_keys.pop(session_id, None)
        self._approval_time.pop(session_id, None)

    def _build_signed_response_locked(
        self,
        *,
        k_poll: bytes,
        response_context: str,
        session_id: str,
        sequence: int,
        request_nonce: bytes,
        status: str,
        expires_in: int,
        device_id: str,
        credential_token: str | None,
        http_status: int = 200,
    ) -> dict[str, Any]:
        """Build a status payload whose MAC covers every field the client consumes."""
        credential_state = "delivered" if credential_token else "none"
        credential_sha256 = (
            compute_credential_sha256(credential_token) if credential_token else b""
        )

        canonical_response = build_canonical_poll_response(
            version=PAIRING_PROTOCOL_VERSION,
            response_context=response_context,
            session_id=session_id,
            http_status=http_status,
            sequence=sequence,
            request_nonce=request_nonce,
            status=status,
            expires_in=expires_in,
            device_id=device_id,
            credential_state=credential_state,
            credential_sha256=credential_sha256,
        )
        response_sig = compute_poll_response_signature(k_poll, canonical_response)

        payload: dict[str, Any] = {
            "response_context": response_context,
            "http_status": http_status,
            "session_id": session_id,
            "sequence": sequence,
            "status": status,
            "expires_in": expires_in,
            "device_id": device_id,
            "credential_state": credential_state,
            "credential_sha256": credential_sha256.hex(),
            "response_sig": response_sig.hex(),
        }
        if credential_token:
            payload["session_token"] = credential_token
        return payload

    def build_handshake_response(
        self, session_id: str, client_nonce: bytes
    ) -> dict[str, Any] | None:
        """Build the signed response for a successful blocking handshake.

        Handshake responses use sequence ``0`` and the verified client nonce so
        they cannot be confused with any poll response (which starts at 1).
        """
        with self._lock:
            self._cleanup_expired_locked()
            now = time.time()

            if self._state != BridgeState.PAIRED or self._active_session is None:
                return None
            if self._active_session.session_id != session_id:
                return None

            k_poll = self._poll_keys.get(session_id)
            if not k_poll:
                return None

            return self._build_signed_response_locked(
                k_poll=k_poll,
                response_context="handshake",
                session_id=session_id,
                sequence=0,
                request_nonce=client_nonce,
                status="approved",
                expires_in=max(0, int(self._active_session.expires_at - now)),
                device_id=self._active_session.client_id,
                credential_token=self._active_session.bearer_token,
            )

    def check_authenticated_poll_status(
        self,
        session_id: str,
        sequence: int,
        request_nonce: bytes,
        timestamp: float,
        signature: bytes,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Check status with per-request fresh authentication and replay rejection."""
        with self._lock:
            self._cleanup_expired_locked()
            now = time.time()

            k_poll = self._poll_keys.get(session_id)
            if not k_poll:
                return False, "unknown_or_expired_polling", None

            # 1. Freshness window check (+/- 60s)
            if abs(now - timestamp) > 60.0:
                return False, "timestamp_out_of_bounds", None

            # 2. Verify the request MAC before mutating any replay state.
            canonical_poll = build_canonical_poll_request(
                version=PAIRING_PROTOCOL_VERSION,
                session_id=session_id,
                sequence=sequence,
                request_nonce=request_nonce,
                timestamp=timestamp,
            )
            if not verify_poll_signature(k_poll, canonical_poll, signature):
                return False, "invalid_signature", None

            # 3. Strictly monotonic sequence number
            last_seq = self._poll_sequences.get(session_id, 0)
            if sequence <= last_seq:
                return False, "replay_sequence_invalid", None

            # 4. Request nonce uniqueness (bounded memory)
            seen_nonces = self._poll_nonces.setdefault(session_id, set())
            if request_nonce in seen_nonces:
                return False, "replay_nonce_detected", None
            seen_nonces.add(request_nonce)
            if len(seen_nonces) > 50:
                seen_nonces.pop()

            self._poll_sequences[session_id] = sequence

            # 5. State dispatch
            if self._state == BridgeState.CONFIRMING and self._pending_request:
                if self._pending_request.session_id == session_id:
                    data = self._build_signed_response_locked(
                        k_poll=k_poll,
                        response_context="poll",
                        session_id=session_id,
                        sequence=sequence,
                        request_nonce=request_nonce,
                        status="pending_approval",
                        expires_in=0,
                        device_id=self._pending_request.client_id,
                        credential_token=None,
                    )
                    return True, "pending_approval", data

            if self._state == BridgeState.PAIRED and self._active_session:
                if self._active_session.session_id == session_id:
                    approval_time = self._approval_time.get(session_id, 0.0)
                    if now - approval_time > APPROVAL_RECOVERY_WINDOW:
                        # Recovery window closed; destroy all polling material.
                        self._discard_poll_state_locked(session_id)
                        return False, "polling_window_closed", None

                    data = self._build_signed_response_locked(
                        k_poll=k_poll,
                        response_context="poll",
                        session_id=session_id,
                        sequence=sequence,
                        request_nonce=request_nonce,
                        status="approved",
                        expires_in=max(0, int(self._active_session.expires_at - now)),
                        device_id=self._active_session.client_id,
                        credential_token=self._active_session.bearer_token,
                    )
                    return True, "approved", data

            if self._state == BridgeState.REJECTED:
                self._discard_poll_state_locked(session_id)
                return False, "rejected", None

            if self._state == BridgeState.EXPIRED:
                self._discard_poll_state_locked(session_id)
                return False, "expired", None

            return False, "unknown_session", None

    def reset_locked(self) -> None:
        self._state = BridgeState.IDLE
        self._current_offer = None
        self._pending_request = None
        self._active_session = None
        self._poll_keys.clear()
        self._poll_sequences.clear()
        self._poll_nonces.clear()
        self._cached_session_keys.clear()
        self._approval_time.clear()
        self._ip_lockouts.clear()
        self._ip_failed_proofs.clear()
        self._total_failed_proofs = 0
        self._failed_proof_count = 0
        self._failed_attempts.clear()
        self._approval_event.set()

    def _cleanup_expired_locked(self) -> None:
        now = time.time()
        if self._state == BridgeState.OFFERING and self._current_offer and self._current_offer.is_expired(now):
            self._state = BridgeState.EXPIRED
            self._consumed_session_ids.add(self._current_offer.session_id)
            self._discard_poll_state_locked(self._current_offer.session_id)
            self._current_offer = None
        elif self._state == BridgeState.CONFIRMING and self._pending_request and self._pending_request.is_expired(now):
            self._state = BridgeState.EXPIRED
            self._consumed_session_ids.add(self._pending_request.session_id)
            self._discard_poll_state_locked(self._pending_request.session_id)
            self._pending_request = None
            self._current_offer = None
            self._approval_event.set()
        elif self._state == BridgeState.PAIRED and self._active_session and self._active_session.is_expired(now):
            self._state = BridgeState.EXPIRED
            self._discard_poll_state_locked(self._active_session.session_id)
            self._active_session = None

    def _record_failed_attempt(self, ip: str, timestamp: float) -> None:
        # Bounded rate limiting memory: purge stale entries and enforce hard cap
        if len(self._failed_attempts) >= MAX_TRACKED_RATE_LIMIT_IPS:
            self._purge_stale_ips(timestamp)
            while len(self._failed_attempts) >= MAX_TRACKED_RATE_LIMIT_IPS:
                # Evict oldest tracked IP
                oldest_ip = min(
                    self._failed_attempts.keys(),
                    key=lambda k: self._failed_attempts[k][-1] if self._failed_attempts[k] else 0.0,
                )
                del self._failed_attempts[oldest_ip]

        attempts = self._failed_attempts.setdefault(ip, [])
        attempts.append(timestamp)
        self._failed_attempts[ip] = [t for t in attempts if timestamp - t < RATE_LIMIT_WINDOW_SECONDS]

    def _purge_stale_ips(self, now: float) -> None:
        stale = [
            ip for ip, att in self._failed_attempts.items()
            if not att or (now - att[-1] >= RATE_LIMIT_WINDOW_SECONDS)
        ]
        for ip in stale:
            del self._failed_attempts[ip]


__all__ = ["PairingManager"]
