"""Strict validation for phone-to-desktop upload metadata.

Names are display metadata only.  They are never used as quarantine paths and
are revalidated immediately before publication.  MIME types are stored as
untrusted hints.
"""
from __future__ import annotations

import re
import unicodedata

TRANSFER_PROTOCOL_VERSION = "1"

MIB = 1024 * 1024
GIB = 1024 * MIB

TRANSFER_CHUNK_BYTES = 1 * MIB
MAX_UPLOAD_BYTES = 2 * GIB
MAX_AGGREGATE_RESERVED_BYTES = 2 * GIB
MAX_CONCURRENT_TRANSFERS = 2
MAX_ACTIVE_TRANSFERS_PER_SESSION = 2
MAX_RETAINED_TRANSFERS_PER_SESSION = 8
MAX_TRANSFER_REGISTRY = 32
TRANSFER_APPROVAL_TTL_SECONDS = 60.0
FINAL_ACCEPTANCE_TTL_SECONDS = 900.0
TRANSFER_STALLED_TIMEOUT_SECONDS = 30.0
TRANSFER_RECEIVE_TIMEOUT_SECONDS = 30.0
TRANSFER_TIMESTAMP_TOLERANCE_SECONDS = 60.0
TRANSFER_REPLAY_WINDOW = 128
TRANSFER_REPLAY_NONCE_LIMIT = 256
TERMINAL_RETENTION_SECONDS = 300.0

MAX_FILENAME_CHARS = 128
MAX_FILENAME_BYTES = 255
MAX_MIME_CHARS = 128
MAX_CONTROL_BODY_BYTES = 8 * 1024

TRANSFER_ID_PATTERN = re.compile(r"^tr_[0-9a-f]{24}$")
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
MIME_PATTERN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:\s*;\s*[A-Za-z0-9=._-]+)*$")

BIDI_CONTROL_CODEPOINTS = frozenset(
    {0x061C, 0x200E, 0x200F}
    | set(range(0x202A, 0x202F))
    | set(range(0x2066, 0x206A))
)

TRANSFER_DECISIONS = frozenset(
    {"allow_receive", "reject_receive", "accept_file", "discard_file"}
)


class UploadValidationError(ValueError):
    """A structured metadata validation refusal."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def validate_display_filename(value: object) -> str:
    """Normalize and validate a display-only filename.

    Path separators, NUL, C0/C1 controls, Unicode line/paragraph separators,
    surrogate code points, and bidi control characters are rejected.  The
    result is NFC-normalized and is never used as a filesystem path.
    """
    if not isinstance(value, str):
        raise UploadValidationError("invalid_filename", "filename must be a string")
    if not value:
        raise UploadValidationError("invalid_filename", "filename must not be empty")
    if len(value) > MAX_FILENAME_CHARS:
        raise UploadValidationError(
            "invalid_filename", "filename exceeds 128 characters"
        )
    if value != value.strip():
        raise UploadValidationError(
            "invalid_filename", "filename must not start or end with whitespace"
        )
    if value in (".", ".."):
        raise UploadValidationError("invalid_filename", "filename is reserved")

    for char in value:
        codepoint = ord(char)
        if char in ("/", "\\", "\x00"):
            raise UploadValidationError(
                "invalid_filename", "filename must not contain path separators"
            )
        if codepoint in BIDI_CONTROL_CODEPOINTS:
            raise UploadValidationError(
                "invalid_filename", "filename must not contain bidi controls"
            )
        category = unicodedata.category(char)
        if category in ("Cc", "Cs", "Zl", "Zp"):
            raise UploadValidationError(
                "invalid_filename", "filename must not contain control characters"
            )

    normalized = unicodedata.normalize("NFC", value)
    if len(normalized) > MAX_FILENAME_CHARS:
        raise UploadValidationError(
            "invalid_filename", "filename exceeds 128 characters after normalization"
        )
    if len(normalized.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise UploadValidationError(
            "invalid_filename", "filename exceeds the filesystem byte limit"
        )
    if normalized in (".", "..") or not normalized.strip():
        raise UploadValidationError("invalid_filename", "filename is reserved")
    return normalized


def validate_declared_size(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise UploadValidationError("invalid_size", "size must be an integer")
    if value < 0:
        raise UploadValidationError("invalid_size", "size must be non-negative")
    if value > MAX_UPLOAD_BYTES:
        raise UploadValidationError(
            "size_exceeds_limit", "file exceeds the 2 GiB upload limit"
        )
    return value


def validate_sha256_hex(value: object) -> bytes:
    if not isinstance(value, str) or not SHA256_HEX_PATTERN.fullmatch(value):
        raise UploadValidationError(
            "invalid_sha256", "sha256 must be exactly 64 hexadecimal characters"
        )
    return bytes.fromhex(value)


def validate_sha256_bytes(value: object) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
        raise UploadValidationError("invalid_sha256", "sha256 must be exactly 32 bytes")
    return bytes(value)


def validate_mime_hint(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise UploadValidationError("invalid_mime", "mime must be a string or null")
    if not value:
        return None
    if len(value) > MAX_MIME_CHARS:
        raise UploadValidationError("invalid_mime", "mime hint is too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise UploadValidationError("invalid_mime", "mime hint contains controls")
    if not MIME_PATTERN.fullmatch(value):
        raise UploadValidationError("invalid_mime", "mime hint is malformed")
    return value


def validate_transfer_id(value: object) -> str:
    if not isinstance(value, str) or not TRANSFER_ID_PATTERN.fullmatch(value):
        raise UploadValidationError("invalid_transfer_id", "transfer id is malformed")
    return value


def validate_decision(value: object) -> str:
    if not isinstance(value, str) or value not in TRANSFER_DECISIONS:
        raise UploadValidationError(
            "invalid_decision", "decision is not a documented transfer decision"
        )
    return value


__all__ = [
    "TRANSFER_PROTOCOL_VERSION",
    "MIB",
    "GIB",
    "TRANSFER_CHUNK_BYTES",
    "MAX_UPLOAD_BYTES",
    "MAX_AGGREGATE_RESERVED_BYTES",
    "MAX_CONCURRENT_TRANSFERS",
    "MAX_ACTIVE_TRANSFERS_PER_SESSION",
    "MAX_RETAINED_TRANSFERS_PER_SESSION",
    "MAX_TRANSFER_REGISTRY",
    "TRANSFER_APPROVAL_TTL_SECONDS",
    "FINAL_ACCEPTANCE_TTL_SECONDS",
    "TRANSFER_STALLED_TIMEOUT_SECONDS",
    "TRANSFER_RECEIVE_TIMEOUT_SECONDS",
    "TRANSFER_TIMESTAMP_TOLERANCE_SECONDS",
    "TRANSFER_REPLAY_WINDOW",
    "TRANSFER_REPLAY_NONCE_LIMIT",
    "TERMINAL_RETENTION_SECONDS",
    "MAX_FILENAME_CHARS",
    "MAX_MIME_CHARS",
    "MAX_CONTROL_BODY_BYTES",
    "TRANSFER_DECISIONS",
    "UploadValidationError",
    "validate_display_filename",
    "validate_declared_size",
    "validate_sha256_hex",
    "validate_sha256_bytes",
    "validate_mime_hint",
    "validate_transfer_id",
    "validate_decision",
]
