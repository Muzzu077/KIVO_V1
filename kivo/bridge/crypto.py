"""Cryptographic primitives, certificate generation, and SAS derivation for Phone Bridge."""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import ipaddress
import struct
from typing import Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand
from cryptography.x509.oid import NameOID

SAS_MODULO = 1_000_000
# Rejection sampling ceiling: 2^32 - (2^32 % 1,000,000) = 4,294,000,000
SAS_UNBIASED_CEILING = 4_294_000_000


def generate_ephemeral_certificate(
    lan_ip: str,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey, str, bytes, bytes]:
    """Generate in-memory ephemeral ECDSA P-256 TLS certificate valid for 24 hours.

    Returns:
        (cert, private_key, cert_fingerprint_hex, cert_pem_bytes, key_pem_bytes)
    """
    key = ec.generate_private_key(ec.SECP256R1())
    ip_obj = ipaddress.IPv4Address(lan_ip)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "kivo-bridge.local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "KIVO Desktop"),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=24))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ip_obj),
                x509.DNSName("kivo-bridge.local"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    der_bytes = cert.public_bytes(serialization.Encoding.DER)
    fp_hex = hashlib.sha256(der_bytes).hexdigest().upper()

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    return cert, key, fp_hex, cert_pem, key_pem


def _pack_field(data: bytes | str) -> bytes:
    b = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    return struct.pack(">I", len(b)) + b


def build_canonical_context(
    version: str,
    session_id: str,
    cert_fp: str,
    server_nonce: bytes,
    client_nonce: bytes,
    client_id: str,
    algorithm_id: str,
) -> bytes:
    """Build an unambiguous length-prefixed binary pairing transcript."""
    return (
        _pack_field(version)
        + _pack_field(session_id)
        + _pack_field(cert_fp.upper())
        + _pack_field(server_nonce)
        + _pack_field(client_nonce)
        + _pack_field(client_id)
        + _pack_field(algorithm_id)
    )


def compute_client_proof(pairing_secret: bytes, context_bytes: bytes) -> bytes:
    """Compute HMAC-SHA256 client proof over pairing context."""
    mac = hmac.new(
        pairing_secret,
        b"KIVO_BRIDGE_CLIENT_PROOF_V1\x00" + context_bytes,
        hashlib.sha256,
    )
    return mac.digest()


def verify_client_proof(
    pairing_secret: bytes, context_bytes: bytes, received_proof: bytes
) -> bool:
    """Verify client proof using constant-time comparison."""
    expected = compute_client_proof(pairing_secret, context_bytes)
    return hmac.compare_digest(expected, received_proof)


def derive_sas_code(
    pairing_secret: bytes,
    server_nonce: bytes,
    context_bytes: bytes,
) -> tuple[int, str]:
    """Derive an unbiased 6-digit Short Authentication String via HKDF-SHA256.

    Returns:
        (sas_int: int, formatted_str: str) e.g. (482910, "482 910")
    """
    counter = 0
    while True:
        info = (
            b"kivo-bridge-sas-v1\x00"
            + struct.pack(">I", counter)
            + context_bytes
        )
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=4,
            salt=server_nonce,
            info=info,
        )
        derived = hkdf.derive(pairing_secret)
        num = struct.unpack(">I", derived)[0]
        # Rejection sampling for unbiased modulo
        if num < SAS_UNBIASED_CEILING:
            sas_int = num % SAS_MODULO
            raw_str = f"{sas_int:06d}"
            formatted = f"{raw_str[:3]} {raw_str[3:]}"
            return sas_int, formatted
        counter += 1


def derive_session_keys(
    pairing_secret: bytes,
    client_nonce: bytes,
    context_bytes: bytes,
) -> tuple[bytes, bytes, str]:
    """Derive directional session authentication keys and bearer token via HKDF.

    Returns:
        (upstream_key_bytes, downstream_key_bytes, bearer_token_str)
    """
    hkdf_extract = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=client_nonce,
        info=b"kivo-bridge-prk-v1",
    )
    prk = hkdf_extract.derive(pairing_secret)

    k_up = HKDFExpand(
        algorithm=hashes.SHA256(),
        length=32,
        info=b"kivo-session-upstream-v1\x00" + context_bytes,
    ).derive(prk)

    k_down = HKDFExpand(
        algorithm=hashes.SHA256(),
        length=32,
        info=b"kivo-session-downstream-v1\x00" + context_bytes,
    ).derive(prk)

    token_bytes = HKDFExpand(
        algorithm=hashes.SHA256(),
        length=32,
        info=b"kivo-session-bearer-v1\x00" + context_bytes,
    ).derive(prk)

    bearer_token = "sess_tok_" + base64.urlsafe_b64encode(token_bytes).decode("ascii").rstrip("=")

    return k_up, k_down, bearer_token


def derive_poll_key(
    pairing_secret: bytes,
    client_nonce: bytes,
    context_bytes: bytes,
) -> bytes:
    """Derive dedicated polling authentication key K_poll via HKDF."""
    hkdf_extract = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=client_nonce,
        info=b"kivo-bridge-prk-v1",
    )
    prk = hkdf_extract.derive(pairing_secret)

    k_poll = HKDFExpand(
        algorithm=hashes.SHA256(),
        length=32,
        info=b"kivo-pair-poll-v1\x00" + context_bytes,
    ).derive(prk)

    return k_poll


def build_canonical_poll_request(
    version: str,
    session_id: str,
    sequence: int,
    request_nonce: bytes,
    timestamp: float,
    method: str = "POST",
    endpoint: str = "/api/v1/pair/status",
) -> bytes:
    """Build unambiguous length-prefixed binary encoding of a poll request."""
    return (
        _pack_field(version)
        + _pack_field(session_id)
        + _pack_field(method)
        + _pack_field(endpoint)
        + struct.pack(">Q", sequence)
        + _pack_field(request_nonce)
        + struct.pack(">d", timestamp)
    )


def compute_poll_signature(k_poll: bytes, canonical_poll_bytes: bytes) -> bytes:
    """Compute HMAC-SHA256 signature over canonical poll request."""
    mac = hmac.new(
        k_poll,
        b"KIVO_BRIDGE_POLL_REQUEST_V1\x00" + canonical_poll_bytes,
        hashlib.sha256,
    )
    return mac.digest()


def verify_poll_signature(
    k_poll: bytes, canonical_poll_bytes: bytes, received_sig: bytes
) -> bool:
    """Verify poll request signature using constant-time comparison."""
    expected = compute_poll_signature(k_poll, canonical_poll_bytes)
    return hmac.compare_digest(expected, received_sig)


def compute_credential_sha256(credential: str) -> bytes:
    """Hash a session credential for inclusion in the signed response transcript.

    The hash binds the exact credential string returned in the response body to
    the response MAC without placing the credential itself inside the transcript.
    """
    return hashlib.sha256(credential.encode("utf-8")).digest()


def build_canonical_poll_response(
    version: str,
    response_context: str,
    session_id: str,
    http_status: int,
    sequence: int,
    request_nonce: bytes,
    status: str,
    expires_in: int,
    device_id: str,
    credential_state: str,
    credential_sha256: bytes,
) -> bytes:
    """Build unambiguous length-prefixed binary encoding of a poll response.

    ``response_context`` is ``"poll"`` for status polling or ``"handshake"``
    for the blocking handshake response. ``sequence`` is ``0`` and
    ``request_nonce`` is the client nonce for handshake responses.
    """
    if not 0 <= http_status <= 0xFFFF:
        raise ValueError("http_status out of range")
    if sequence < 0:
        raise ValueError("sequence must be non-negative")
    if expires_in < 0:
        raise ValueError("expires_in must be non-negative")
    if len(credential_sha256) not in (0, 32):
        raise ValueError("credential_sha256 must be empty or 32 bytes")

    return (
        _pack_field(version)
        + _pack_field(response_context)
        + _pack_field(session_id)
        + struct.pack(">H", http_status)
        + struct.pack(">Q", sequence)
        + _pack_field(request_nonce)
        + _pack_field(status)
        + struct.pack(">Q", expires_in)
        + _pack_field(device_id)
        + _pack_field(credential_state)
        + _pack_field(credential_sha256)
    )


def compute_poll_response_signature(
    k_poll: bytes, canonical_response_bytes: bytes
) -> bytes:
    """Compute HMAC-SHA256 over the complete canonical response transcript."""
    mac = hmac.new(
        k_poll,
        b"KIVO_BRIDGE_POLL_RESPONSE_V2\x00" + canonical_response_bytes,
        hashlib.sha256,
    )
    return mac.digest()


def verify_poll_response_signature(
    k_poll: bytes, canonical_response_bytes: bytes, received_sig: bytes
) -> bool:
    """Verify a response signature using constant-time comparison."""
    expected = compute_poll_response_signature(k_poll, canonical_response_bytes)
    return hmac.compare_digest(expected, received_sig)


__all__ = [
    "generate_ephemeral_certificate",
    "build_canonical_context",
    "compute_client_proof",
    "verify_client_proof",
    "derive_sas_code",
    "derive_session_keys",
    "derive_poll_key",
    "build_canonical_poll_request",
    "compute_poll_signature",
    "verify_poll_signature",
    "compute_credential_sha256",
    "build_canonical_poll_response",
    "compute_poll_response_signature",
    "verify_poll_response_signature",
]
