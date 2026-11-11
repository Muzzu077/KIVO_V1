"""Canonical authentication transcripts for Stage 13.3.3.1 file transfers.

The transfer protocol is deliberately separate from the pairing/polling
transcript.  Requests are authenticated with the session's upstream key
(``K_up``) and responses with the downstream key (``K_down``).  The bearer
token only identifies the session; it is never a MAC key.

Every field is length-prefixed so that moving bytes between fields cannot
produce the same transcript.  Integers use fixed-width big-endian encodings and
timestamps use an IEEE-754 double, matching the existing pairing primitives.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import math
import struct

TRANSFER_PROTOCOL_VERSION = "1"

TRANSFER_REQUEST_LABEL = b"KIVO_BRIDGE_TRANSFER_REQUEST_V1\x00"
TRANSFER_RESPONSE_LABEL = b"KIVO_BRIDGE_TRANSFER_RESPONSE_V1\x00"

TRANSFER_NONCE_BYTES = 16
TRANSFER_RESPONSE_CONTEXTS = frozenset(
    {
        "upload_init",
        "upload_status",
        "upload_chunk",
        "upload_cancel",
        "upload_decision",
        "download_init",
        "download_status",
        "download_chunk",
        "download_complete",
        "download_cancel",
    }
)


def _pack_field(data: bytes | str) -> bytes:
    value = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    return struct.pack(">I", len(value)) + value


def decode_nonce_b64(value: object) -> bytes:
    """Decode a strict unpadded base64url transfer nonce (exactly 16 bytes)."""
    if not isinstance(value, str) or not value:
        raise ValueError("nonce must be a non-empty string")
    if len(value) > 64:
        raise ValueError("nonce encoding is too long")
    if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in value):
        raise ValueError("nonce must use unpadded base64url characters")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001 - normalize decoder failures
        raise ValueError("nonce is not valid base64url") from exc
    if len(raw) != TRANSFER_NONCE_BYTES:
        raise ValueError("nonce must be exactly 16 bytes")
    return raw


def encode_nonce_b64(nonce: bytes) -> str:
    if len(nonce) != TRANSFER_NONCE_BYTES:
        raise ValueError("nonce must be exactly 16 bytes")
    return base64.urlsafe_b64encode(nonce).decode("ascii").rstrip("=")


def build_canonical_transfer_request(
    *,
    version: str,
    session_id: str,
    method: str,
    endpoint: str,
    sequence: int,
    nonce: bytes,
    timestamp: float,
    content_length: int,
    content_sha256: bytes,
) -> bytes:
    """Build the unambiguous transcript authenticated by ``K_up``."""
    if not isinstance(version, str) or not version:
        raise ValueError("version must be a non-empty string")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(method, str) or not method or method.upper() != method:
        raise ValueError("method must be an uppercase non-empty string")
    if not isinstance(endpoint, str) or not endpoint.startswith("/"):
        raise ValueError("endpoint must be an absolute request path")
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise ValueError("sequence must be an integer")
    if not 1 <= sequence <= 2**63 - 1:
        raise ValueError("sequence out of range")
    if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != TRANSFER_NONCE_BYTES:
        raise ValueError("nonce must be exactly 16 bytes")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        raise ValueError("timestamp must be numeric")
    if not math.isfinite(float(timestamp)):
        raise ValueError("timestamp must be finite")
    if not 0.0 <= float(timestamp) <= 4102444800.0:
        raise ValueError("timestamp out of range")
    if not isinstance(content_length, int) or isinstance(content_length, bool):
        raise ValueError("content_length must be an integer")
    if not 0 <= content_length <= 2**63 - 1:
        raise ValueError("content_length out of range")
    if not isinstance(content_sha256, (bytes, bytearray)) or len(content_sha256) != 32:
        raise ValueError("content_sha256 must be exactly 32 bytes")

    return (
        _pack_field(version)
        + _pack_field(session_id)
        + _pack_field(method)
        + _pack_field(endpoint)
        + struct.pack(">Q", sequence)
        + _pack_field(bytes(nonce))
        + struct.pack(">d", float(timestamp))
        + struct.pack(">Q", content_length)
        + _pack_field(bytes(content_sha256))
    )


def compute_transfer_request_signature(k_up: bytes, canonical_request: bytes) -> bytes:
    """Compute HMAC-SHA256 over the canonical Android-to-desktop request."""
    return hmac.new(
        k_up,
        TRANSFER_REQUEST_LABEL + canonical_request,
        hashlib.sha256,
    ).digest()


def verify_transfer_request_signature(
    k_up: bytes, canonical_request: bytes, received_signature: bytes
) -> bool:
    expected = compute_transfer_request_signature(k_up, canonical_request)
    return hmac.compare_digest(expected, bytes(received_signature))


def build_canonical_transfer_response(
    *,
    version: str,
    response_context: str,
    session_id: str,
    http_status: int,
    sequence: int,
    nonce: bytes,
    state: str,
    transfer_id: str,
    payload_length: int,
    payload_sha256: bytes,
) -> bytes:
    """Build the transcript authenticated by ``K_down`` for a transfer reply."""
    if response_context not in TRANSFER_RESPONSE_CONTEXTS:
        raise ValueError("unknown response context")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(http_status, int) or isinstance(http_status, bool):
        raise ValueError("http_status must be an integer")
    if not 100 <= http_status <= 599:
        raise ValueError("http_status out of range")
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise ValueError("sequence must be an integer")
    if not 1 <= sequence <= 2**63 - 1:
        raise ValueError("sequence out of range")
    if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != TRANSFER_NONCE_BYTES:
        raise ValueError("nonce must be exactly 16 bytes")
    if not isinstance(state, str) or not state:
        raise ValueError("state must be a non-empty string")
    if not isinstance(transfer_id, str):
        raise ValueError("transfer_id must be a string")
    if not isinstance(payload_length, int) or isinstance(payload_length, bool):
        raise ValueError("payload_length must be an integer")
    if not 0 <= payload_length <= 2**63 - 1:
        raise ValueError("payload_length out of range")
    if not isinstance(payload_sha256, (bytes, bytearray)) or len(payload_sha256) != 32:
        raise ValueError("payload_sha256 must be exactly 32 bytes")

    return (
        _pack_field(version)
        + _pack_field(response_context)
        + _pack_field(session_id)
        + struct.pack(">H", http_status)
        + struct.pack(">Q", sequence)
        + _pack_field(bytes(nonce))
        + _pack_field(state)
        + _pack_field(transfer_id)
        + struct.pack(">Q", payload_length)
        + _pack_field(bytes(payload_sha256))
    )


def compute_transfer_response_signature(
    k_down: bytes, canonical_response: bytes
) -> bytes:
    """Compute HMAC-SHA256 over the canonical desktop-to-Android response."""
    return hmac.new(
        k_down,
        TRANSFER_RESPONSE_LABEL + canonical_response,
        hashlib.sha256,
    ).digest()


def verify_transfer_response_signature(
    k_down: bytes, canonical_response: bytes, received_signature: bytes
) -> bool:
    expected = compute_transfer_response_signature(k_down, canonical_response)
    return hmac.compare_digest(expected, bytes(received_signature))


__all__ = [
    "TRANSFER_PROTOCOL_VERSION",
    "TRANSFER_NONCE_BYTES",
    "TRANSFER_REQUEST_LABEL",
    "TRANSFER_RESPONSE_LABEL",
    "decode_nonce_b64",
    "encode_nonce_b64",
    "build_canonical_transfer_request",
    "compute_transfer_request_signature",
    "verify_transfer_request_signature",
    "build_canonical_transfer_response",
    "compute_transfer_response_signature",
    "verify_transfer_response_signature",
]
