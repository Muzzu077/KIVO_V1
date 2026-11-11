"""Data models and state definitions for KIVO Phone-to-PC Bridge."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PAIRING_PROTOCOL_VERSION = "1"
ALGORITHM_ID = "HMAC-SHA256-HKDF-SAS-V1"

PAIRING_OFFER_TTL_SECONDS = 300.0       # 5 minutes for QR code
PAIRING_APPROVAL_TTL_SECONDS = 60.0     # 60 seconds to approve on desktop
SESSION_TTL_SECONDS = 7200.0             # 2 hours session lifetime

MAX_TOTAL_FAILED_PROOFS_PER_OFFER = 25  # Global ceiling on offer across all IPs
MAX_FAILED_PROOFS_PER_IP = 5            # Max failed proofs from one IP before lockout
IP_LOCKOUT_SECONDS = 300.0              # 5 minutes lockout for offending IP
MAX_FAILED_ATTEMPTS_PER_IP = 5          # Rate limit per IP
RATE_LIMIT_WINDOW_SECONDS = 60.0        # Rate limit window
MAX_TRACKED_RATE_LIMIT_IPS = 100        # Bounded rate limit memory
APPROVAL_RECOVERY_WINDOW = 30.0         # 30 seconds recovery window for poll completion


class BridgeState(str, Enum):
    IDLE = "idle"
    OFFERING = "offering"
    CONFIRMING = "confirming"
    PAIRED = "paired"
    REJECTED = "rejected"
    REVOKED = "revoked"
    EXPIRED = "expired"
    ERROR = "error"

    # Semantic alias
    AUTHENTICATED_PENDING_APPROVAL = "confirming"


@dataclass(frozen=True)
class PairingOffer:
    """Ephemeral pairing offer generated when starting bridge."""

    session_id: str
    lan_ip: str
    port: int
    cert_fingerprint: str
    pairing_secret: bytes = field(repr=False)
    server_nonce: bytes = field(repr=False)
    qr_payload: str
    qr_data_url: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default=0.0)

    def is_expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return current >= self.expires_at


@dataclass(frozen=True)
class PairingRequest:
    """Pending client pairing handshake awaiting desktop user approval."""

    session_id: str
    client_id: str
    device_name: str
    client_ip: str
    client_nonce: bytes = field(repr=False)
    sas_code: str
    context_bytes: bytes = field(repr=False)
    received_at: float = field(default_factory=time.time)
    expires_at: float = field(default=0.0)

    def is_expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return current >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "client_id": self.client_id,
            "device_name": self.device_name,
            "client_ip": self.client_ip,
            "sas_code": self.sas_code,
            "expires_at": round(self.expires_at, 3),
        }


@dataclass(frozen=True)
class PairedSession:
    """Established active session with a paired mobile device."""

    session_id: str
    client_id: str
    device_name: str
    client_ip: str
    bearer_token: str = field(repr=False)
    upstream_key: bytes = field(repr=False)
    downstream_key: bytes = field(repr=False)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    expires_at: float = field(default=0.0)
    is_revoked: bool = False

    def is_expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return self.is_revoked or current >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "client_id": self.client_id,
            "device_name": self.device_name,
            "client_ip": self.client_ip,
            "created_at": round(self.created_at, 3),
            "last_active": round(self.last_active, 3),
            "expires_at": round(self.expires_at, 3),
            "is_revoked": self.is_revoked,
        }


@dataclass(frozen=True)
class BridgeStatus:
    """Status snapshot of the Phone-to-PC Bridge for IPC."""

    state: str = BridgeState.IDLE.value
    lan_ip: str | None = None
    port: int | None = None
    cert_fingerprint: str | None = None
    qr_data_url: str | None = None
    pending_pairing: dict[str, Any] | None = None
    active_device: dict[str, Any] | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "lan_ip": self.lan_ip,
            "port": self.port,
            "cert_fingerprint": self.cert_fingerprint,
            "qr_data_url": self.qr_data_url,
            "pending_pairing": self.pending_pairing,
            "active_device": self.active_device,
            "error_message": self.error_message,
        }


__all__ = [
    "PAIRING_PROTOCOL_VERSION",
    "ALGORITHM_ID",
    "PAIRING_OFFER_TTL_SECONDS",
    "PAIRING_APPROVAL_TTL_SECONDS",
    "SESSION_TTL_SECONDS",
    "BridgeState",
    "PairingOffer",
    "PairingRequest",
    "PairedSession",
    "BridgeStatus",
]
