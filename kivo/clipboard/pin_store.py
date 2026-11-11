"""Persistent storage for pinned clipboard entries.

Stored at $XDG_DATA_HOME/kivo/clipboard_pins.json with 0600 permissions.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kivo.clipboard.models import (
    ClipItem,
    MAX_HISTORY_ITEMS,
)

PIN_SCHEMA_VERSION = 1


def _default_pin_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    if base and base.strip():
        data_dir = Path(base)
    else:
        data_dir = Path.home() / ".local" / "share"
    return data_dir / "kivo" / "clipboard_pins.json"


class PinStore:
    """Manages persistent pinned clipboard items with atomic writes."""

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path is not None else _default_pin_path()
        self._pins: dict[str, ClipItem] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, ClipItem]:
        """Load pinned items from disk safely."""
        self._pins = {}
        self._loaded = True
        if not self._path.exists():
            return self._pins

        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # Corrupted or unreadable pin file fails safely
            return self._pins

        if not isinstance(payload, dict) or payload.get("version") != PIN_SCHEMA_VERSION:
            return self._pins

        pins_list = payload.get("pins", [])
        if not isinstance(pins_list, list):
            return self._pins

        for entry in pins_list[:MAX_HISTORY_ITEMS]:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", "")).strip()
            if not item_id:
                continue

            item = ClipItem(
                id=item_id,
                raw_id=str(entry.get("raw_id", "")),
                type=str(entry.get("type", "text")),
                preview=str(entry.get("preview", "")),
                char_count=int(entry.get("char_count", 0)),
                size_bytes=int(entry.get("size_bytes", 0)),
                timestamp=float(entry.get("timestamp", 0.0)),
                is_pinned=True,
                is_sensitive=False,  # Sensitive items are never saved as pins
                mime_type=str(entry.get("mime_type", "text/plain")),
            )
            self._pins[item_id] = item

        return self._pins

    def save(self) -> bool:
        """Atomically persist pinned items with 0600 mode."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError:
            pass

        payload = {
            "version": PIN_SCHEMA_VERSION,
            "pins": [item.to_dict() for item in self._pins.values()],
        }

        target_dir = self._path.parent
        fd, tmp_path = tempfile.mkstemp(
            prefix=".clipboard_pins_", suffix=".tmp", dir=str(target_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._path)
            return True
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False

    def get_all(self) -> list[ClipItem]:
        if not self._loaded:
            self.load()
        return list(self._pins.values())

    def is_pinned(self, item_id: str) -> bool:
        if not self._loaded:
            self.load()
        return item_id in self._pins

    def add_pin(self, item: ClipItem) -> bool:
        """Pin an item. Rejects sensitive items."""
        if item.is_sensitive:
            raise ValueError("Cannot pin sensitive clipboard item")
        if not self._loaded:
            self.load()
        if len(self._pins) >= MAX_HISTORY_ITEMS and item.id not in self._pins:
            return False

        pinned_item = ClipItem(
            id=item.id,
            raw_id=item.raw_id,
            type=item.type,
            preview=item.preview,
            char_count=item.char_count,
            size_bytes=item.size_bytes,
            timestamp=item.timestamp,
            is_pinned=True,
            is_sensitive=False,
            mime_type=item.mime_type,
        )
        self._pins[item.id] = pinned_item
        return self.save()

    def remove_pin(self, item_id: str) -> bool:
        if not self._loaded:
            self.load()
        if item_id in self._pins:
            del self._pins[item_id]
            return self.save()
        return True


__all__ = ["PinStore", "PIN_SCHEMA_VERSION"]
