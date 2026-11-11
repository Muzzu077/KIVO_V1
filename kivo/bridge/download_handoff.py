"""Strict, path-free ``kivo-download://v1`` ticket handoff.

The desktop UI (Stage 13.4) will render this payload as a QR code.  The payload
is deliberately a *capability reference*, not authorization: every download
request still requires the paired session's bearer token and ``K_up`` MAC, and
the desktop binds the opaque ticket to one session, one paired device, and one
authorized file identity.

Only the opaque ticket identifier, the target device identifier, and minimal
display metadata are present.  Filesystem paths, session identifiers, bearer
tokens, session keys, and file bytes are never encoded.  Parsing is strict:
duplicate parameters, unknown parameters, malformed percent escapes, non-
canonical sizes, ticket syntax errors, and oversized payloads are rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from kivo.bridge.download import DownloadTicket
from kivo.bridge.download_validation import (
    DOWNLOAD_ID_PATTERN,
    MAX_DOWNLOAD_BYTES,
    validate_download_id,
)
from kivo.bridge.transfer_validation import (
    UploadValidationError,
    validate_display_filename,
)

DOWNLOAD_HANDOFF_SCHEME = "kivo-download"
DOWNLOAD_HANDOFF_VERSION = "v1"
DOWNLOAD_HANDOFF_PREFIX = f"{DOWNLOAD_HANDOFF_SCHEME}://{DOWNLOAD_HANDOFF_VERSION}?"
DOWNLOAD_HANDOFF_MAX_BYTES = 2048

DEVICE_ID_PATTERN = re.compile(r"^client_[A-Za-z0-9_.-]{1,57}$")
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INTEGER_PATTERN = re.compile(r"^(0|[1-9][0-9]*)$")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_REQUIRED_PARAMS = frozenset({"ticket", "device", "name", "size", "sha256"})


class DownloadHandoffError(ValueError):
    """A structured handoff refusal that never echoes the rejected payload."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DownloadHandoff:
    version: str
    ticket_id: str
    device_id: str
    filename: str
    size: int
    sha256_hex: str

    def to_payload(self) -> str:
        return build_download_handoff(
            ticket_id=self.ticket_id,
            device_id=self.device_id,
            filename=self.filename,
            size=self.size,
            sha256_hex=self.sha256_hex,
        )


def build_download_handoff(
    *,
    ticket_id: str,
    device_id: str,
    filename: str,
    size: int,
    sha256_hex: str,
) -> str:
    """Build the exact versioned QR payload from validated ticket metadata."""
    safe_ticket = _require_ticket(ticket_id)
    safe_device = _require_device(device_id)
    safe_name = _require_filename(filename)
    safe_size = _require_size(size)
    safe_sha = _require_sha256(sha256_hex)
    query = urlencode(
        {
            "ticket": safe_ticket,
            "device": safe_device,
            "name": safe_name,
            "size": str(safe_size),
            "sha256": safe_sha,
        },
        quote_via=quote,
        safe="",
    )
    payload = DOWNLOAD_HANDOFF_PREFIX + query
    if len(payload.encode("utf-8")) > DOWNLOAD_HANDOFF_MAX_BYTES:
        raise DownloadHandoffError("handoff_too_large", "the download handoff is too large")
    return payload


def build_handoff_for_ticket(ticket: DownloadTicket) -> str:
    return build_download_handoff(
        ticket_id=ticket.download_id,
        device_id=ticket.device_id,
        filename=ticket.filename,
        size=ticket.size,
        sha256_hex=ticket.sha256.hex(),
    )


def parse_download_handoff(
    payload: object,
    *,
    expected_device_id: str | None = None,
) -> DownloadHandoff:
    """Parse a bounded handoff and optionally enforce the local device binding."""
    if not isinstance(payload, str) or not payload:
        raise DownloadHandoffError("invalid_handoff", "the download handoff is missing")
    if payload != payload.strip():
        raise DownloadHandoffError("invalid_handoff", "the download handoff has surrounding whitespace")
    if len(payload.encode("utf-8")) > DOWNLOAD_HANDOFF_MAX_BYTES:
        raise DownloadHandoffError("handoff_too_large", "the download handoff is too large")
    if payload.startswith(f"{DOWNLOAD_HANDOFF_SCHEME}://") and not payload.startswith(
        DOWNLOAD_HANDOFF_PREFIX
    ):
        raise DownloadHandoffError(
            "unsupported_version", "the download handoff version is not supported"
        )
    if not payload.startswith(DOWNLOAD_HANDOFF_PREFIX):
        raise DownloadHandoffError("invalid_handoff", "the download handoff scheme is invalid")
    if "#" in payload:
        raise DownloadHandoffError("invalid_handoff", "the download handoff must not contain a fragment")

    query = payload[len(DOWNLOAD_HANDOFF_PREFIX) :]
    if not query:
        raise DownloadHandoffError("invalid_handoff", "the download handoff has no parameters")

    parsed: dict[str, str] = {}
    for part in query.split("&"):
        if not part or part.count("=") != 1:
            raise DownloadHandoffError("invalid_handoff", "the download handoff is malformed")
        raw_key, raw_value = part.split("=", 1)
        if not raw_key or not raw_value:
            raise DownloadHandoffError("invalid_handoff", "the download handoff is malformed")
        key = _strict_decode(raw_key)
        value = _strict_decode(raw_value)
        if key in parsed:
            raise DownloadHandoffError(
                "duplicate_parameter", "the download handoff contains a duplicate parameter"
            )
        parsed[key] = value

    if set(parsed) != _REQUIRED_PARAMS:
        raise DownloadHandoffError("invalid_handoff", "the download handoff parameters are invalid")

    handoff = DownloadHandoff(
        version=DOWNLOAD_HANDOFF_VERSION,
        ticket_id=_require_ticket(parsed["ticket"]),
        device_id=_require_device(parsed["device"]),
        filename=_require_filename(parsed["name"]),
        size=_require_size_text(parsed["size"]),
        sha256_hex=_require_sha256(parsed["sha256"]),
    )
    if expected_device_id is not None:
        expected = _require_device(expected_device_id)
        if handoff.device_id != expected:
            raise DownloadHandoffError(
                "handoff_device_mismatch",
                "the download was authorized for a different paired device",
            )
    return handoff


def _strict_decode(value: str) -> str:
    out = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%":
            if index + 2 >= len(value) or not _PERCENT_ESCAPE.fullmatch(value[index : index + 3]):
                raise DownloadHandoffError("invalid_handoff", "the download handoff encoding is invalid")
            out.append(int(value[index + 1 : index + 3], 16))
            index += 3
            continue
        if char not in _UNRESERVED:
            raise DownloadHandoffError("invalid_handoff", "the download handoff encoding is invalid")
        out.extend(char.encode("utf-8"))
        index += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DownloadHandoffError("invalid_handoff", "the download handoff is not valid UTF-8") from exc


def _require_ticket(value: object) -> str:
    try:
        safe = validate_download_id(value)
    except Exception as exc:  # noqa: BLE001 - normalize validation failure
        raise DownloadHandoffError("invalid_ticket", "the download ticket is malformed") from exc
    if not DOWNLOAD_ID_PATTERN.fullmatch(safe):
        raise DownloadHandoffError("invalid_ticket", "the download ticket is malformed")
    return safe


def _require_device(value: object) -> str:
    if not isinstance(value, str) or not DEVICE_ID_PATTERN.fullmatch(value):
        raise DownloadHandoffError("invalid_device", "the target device identifier is malformed")
    return value


def _require_filename(value: object) -> str:
    try:
        return validate_display_filename(value)
    except UploadValidationError as exc:
        raise DownloadHandoffError("invalid_filename", "the display filename is invalid") from exc


def _require_size(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DownloadHandoffError("invalid_size", "the file size is invalid")
    if not 0 <= value <= MAX_DOWNLOAD_BYTES:
        raise DownloadHandoffError("invalid_size", "the file size is out of range")
    return value


def _require_size_text(value: str) -> int:
    if not _INTEGER_PATTERN.fullmatch(value):
        raise DownloadHandoffError("invalid_size", "the file size is invalid")
    return _require_size(int(value))


def _require_sha256(value: object) -> str:
    if not isinstance(value, str) or not SHA256_HEX_PATTERN.fullmatch(value):
        raise DownloadHandoffError("invalid_sha256", "the file digest is malformed")
    return value


__all__ = [
    "DOWNLOAD_HANDOFF_MAX_BYTES",
    "DOWNLOAD_HANDOFF_PREFIX",
    "DOWNLOAD_HANDOFF_SCHEME",
    "DOWNLOAD_HANDOFF_VERSION",
    "DownloadHandoff",
    "DownloadHandoffError",
    "build_download_handoff",
    "build_handoff_for_ticket",
    "parse_download_handoff",
]
