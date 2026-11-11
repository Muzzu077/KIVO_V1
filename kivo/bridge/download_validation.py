"""Limits and strict validation for desktop-to-Android downloads.

Download tickets are opaque, 256-bit capabilities bound to one paired
session, one file identity, and one logical transfer.  Android clients never
send filesystem paths: the only selectable source is a File Shelf item that the
desktop user has explicitly authorized for export.
"""
from __future__ import annotations

import re

MIB = 1024 * 1024
GIB = 1024 * MIB

DOWNLOAD_CHUNK_BYTES = 1 * MIB
MAX_DOWNLOAD_BYTES = 2 * GIB

# A ticket must be redeemed within five minutes.  Claiming binds a separate,
# finite active-transfer lease: a 2 GiB file needs only ~4.8 Mbps sustained to
# finish inside thirty minutes, while a stalled client still dies at the
# unchanged thirty-second idle timeout.  The lease is never extended.
DOWNLOAD_TICKET_TTL_SECONDS = 300.0
DOWNLOAD_ACTIVE_LEASE_SECONDS = 1800.0
DOWNLOAD_IDLE_TIMEOUT_SECONDS = 30.0

# A download consumes one retained descriptor but no quarantine reservation.
MAX_CONCURRENT_DOWNLOADS = 2
MAX_ACTIVE_DOWNLOADS_PER_SESSION = 8
MAX_DOWNLOAD_REGISTRY = 32
TERMINAL_RETENTION_SECONDS = 300.0

MAX_DOWNLOAD_ID_CHARS = 80
MAX_FILENAME_CHARS = 128
MAX_CONTROL_BODY_BYTES = 8 * 1024

DOWNLOAD_ID_PATTERN = re.compile(r"^tkt_[0-9a-f]{64}$")
SHELF_ITEM_ID_PATTERN = re.compile(r"^shelf_[0-9a-f]{12}$")


class DownloadValidationError(ValueError):
    """Raised when a download identifier or parameter fails validation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def total_chunks(size: int) -> int:
    """Return the exact number of fixed-size chunks for a byte length."""
    if size <= 0:
        return 0
    return (size + DOWNLOAD_CHUNK_BYTES - 1) // DOWNLOAD_CHUNK_BYTES


def chunk_length(size: int, chunk_index: int) -> int:
    """Return the exact byte length of one chunk in a file geometry."""
    if chunk_index < 0 or chunk_index >= total_chunks(size):
        raise DownloadValidationError("invalid_chunk_index", "chunk index is outside the file")
    offset = chunk_index * DOWNLOAD_CHUNK_BYTES
    return min(DOWNLOAD_CHUNK_BYTES, size - offset)


def validate_download_id(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_DOWNLOAD_ID_CHARS:
        raise DownloadValidationError("invalid_download_id", "download_id is malformed")
    if not DOWNLOAD_ID_PATTERN.fullmatch(value):
        raise DownloadValidationError("invalid_download_id", "download_id is malformed")
    return value


def validate_shelf_item_id(value: object) -> str:
    if not isinstance(value, str) or not SHELF_ITEM_ID_PATTERN.fullmatch(value):
        raise DownloadValidationError("invalid_item_id", "item_id is malformed")
    return value


def validate_authorize_flag(value: object) -> bool:
    """Require the explicit boolean authorization flag from the desktop UI."""
    if value is not True:
        raise DownloadValidationError(
            "authorization_required",
            "explicit export authorization is required",
        )
    return True


def validate_chunk_index(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DownloadValidationError("invalid_chunk_index", "chunk index must be an integer")
    if value < 0 or value > 2**32 - 1:
        raise DownloadValidationError("invalid_chunk_index", "chunk index is out of range")
    return value


def validate_download_size(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DownloadValidationError("invalid_size", "size must be an integer")
    if value < 0 or value > MAX_DOWNLOAD_BYTES:
        raise DownloadValidationError(
            "file_too_large",
            f"downloads are limited to {MAX_DOWNLOAD_BYTES} bytes",
        )
    return value


__all__ = [
    "DOWNLOAD_ACTIVE_LEASE_SECONDS",
    "DOWNLOAD_CHUNK_BYTES",
    "DOWNLOAD_ID_PATTERN",
    "DOWNLOAD_IDLE_TIMEOUT_SECONDS",
    "DOWNLOAD_TICKET_TTL_SECONDS",
    "MAX_ACTIVE_DOWNLOADS_PER_SESSION",
    "MAX_CONCURRENT_DOWNLOADS",
    "MAX_CONTROL_BODY_BYTES",
    "MAX_DOWNLOAD_BYTES",
    "MAX_DOWNLOAD_REGISTRY",
    "SHELF_ITEM_ID_PATTERN",
    "TERMINAL_RETENTION_SECONDS",
    "DownloadValidationError",
    "chunk_length",
    "total_chunks",
    "validate_authorize_flag",
    "validate_chunk_index",
    "validate_download_id",
    "validate_download_size",
    "validate_shelf_item_id",
]
