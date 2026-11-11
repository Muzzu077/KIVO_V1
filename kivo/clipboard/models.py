"""Data models for KIVO Clipboard Intelligence."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PREVIEW_CHAR_LIMIT = 120
MAX_HISTORY_ITEMS = 100
MAX_TEXT_CONTENT_BYTES = 128 * 1024
MAX_IMAGE_CONTENT_BYTES = 5 * 1024 * 1024


class ClipType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    OTHER = "other"
    UNSUPPORTED = "unsupported"


class PrivacyLevel(str, Enum):
    NORMAL = "normal"
    SUSPICIOUS = "suspicious"
    SENSITIVE = "sensitive"


def sanitize_preview(text: str, limit: int = PREVIEW_CHAR_LIMIT) -> str:
    """Strip C0/C1 and bidi controls, collapse whitespace, and bound length."""
    # Strip bidi overrides (U+202A-U+202E, U+2066-U+2069) and control characters
    stripped = re.sub(r"[\u202a-\u202e\u2066-\u2069\x00-\x1f\x7f-\x9f]", " ", text)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return collapsed[:limit]


@dataclass(frozen=True)
class ClipItem:
    """Metadata representation of a single clipboard history item."""

    id: str
    raw_id: str
    type: str = ClipType.TEXT.value
    preview: str = ""
    char_count: int = 0
    size_bytes: int = 0
    timestamp: float = field(default_factory=time.time)
    is_pinned: bool = False
    is_sensitive: bool = False
    privacy_level: str = PrivacyLevel.NORMAL.value
    mime_type: str = "text/plain"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "raw_id": self.raw_id,
            "type": self.type,
            "preview": self.preview,
            "char_count": self.char_count,
            "size_bytes": self.size_bytes,
            "timestamp": round(self.timestamp, 3),
            "is_pinned": self.is_pinned,
            "is_sensitive": self.is_sensitive,
            "privacy_level": self.privacy_level,
            "mime_type": self.mime_type,
        }


@dataclass(frozen=True)
class ClipDetail:
    """Detailed item representation returned upon explicit user request."""

    item: ClipItem
    text_content: str | None = None
    has_binary_data: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = self.item.to_dict()
        if self.item.is_sensitive:
            result["text_content"] = "••••••••••••"
        else:
            result["text_content"] = self.text_content
        result["has_binary_data"] = self.has_binary_data
        return result


__all__ = [
    "ClipType",
    "PrivacyLevel",
    "ClipItem",
    "ClipDetail",
    "PREVIEW_CHAR_LIMIT",
    "MAX_HISTORY_ITEMS",
    "MAX_TEXT_CONTENT_BYTES",
    "MAX_IMAGE_CONTENT_BYTES",
    "sanitize_preview",
]
