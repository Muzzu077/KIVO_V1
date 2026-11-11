"""Immutable data models for KIVO File Shelf.

The File Shelf maintains transient desktop references to files, not physical
copies. Staging a file never reads its full content, never executes it, and
never grants authorization to its parent directory.
"""
from __future__ import annotations

import mimetypes
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

MAX_SHELF_ITEMS = 50
MAX_BATCH_URIS = 20
MAX_URI_BYTES = 4096
MAX_FILENAME_LENGTH = 128

_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.\ -]{1,128}$")


def sanitize_filename(name: str, limit: int = MAX_FILENAME_LENGTH) -> str:
    """Sanitize display filename by stripping control and bidi override characters."""
    # Strip bidi overrides (U+202A-U+202E, U+2066-U+2069) and C0/C1 control characters
    stripped = re.sub(r"[\u202a-\u202e\u2066-\u2069\x00-\x1f\x7f-\x9f]", "_", name)
    # Remove path traversal characters or separators from filename
    cleaned = os.path.basename(stripped).strip()
    if not cleaned or cleaned in (".", ".."):
        return "unnamed_file"
    return cleaned[:limit]


def infer_file_type(name: str) -> str:
    """Infer file type from filename extension without reading file contents."""
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    if ext:
        return ext
    mime, _ = mimetypes.guess_type(name)
    if mime:
        return mime.split("/")[-1]
    return "unknown"


@dataclass(frozen=True)
class FileIdentity:
    """Internal filesystem identity recorded at staging to detect modification/replacement."""

    dev: int
    ino: int
    size: int
    mtime_ns: int
    mode: int
    mnt_id: int | None = None


@dataclass(frozen=True, kw_only=True)
class ShelfItem:
    """Metadata representation of a staged file reference."""

    id: str
    name: str
    file_type: str
    size_bytes: int
    staged_at: float = field(default_factory=time.time)
    stale: bool = False
    availability: str = "available"  # "available" | "unavailable"

    # Internal-only attributes: NEVER exposed via ordinary IPC responses
    _canonical_path: str = field(repr=False)
    _identity: FileIdentity = field(repr=False)
    _is_workspace: bool = field(default=False, repr=False)
    _workspace_name: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Serialize safe metadata for IPC consumers.

        Absolute paths and internal inode/device identities are strictly withheld.
        """
        return {
            "id": self.id,
            "name": self.name,
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "staged_at": round(self.staged_at, 3),
            "stale": self.stale,
            "availability": self.availability,
        }


__all__ = [
    "MAX_SHELF_ITEMS",
    "MAX_BATCH_URIS",
    "MAX_URI_BYTES",
    "MAX_FILENAME_LENGTH",
    "FileIdentity",
    "ShelfItem",
    "sanitize_filename",
    "infer_file_type",
]
