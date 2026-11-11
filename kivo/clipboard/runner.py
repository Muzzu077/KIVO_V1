"""Narrowly scoped, bounded wrapper for cliphist and wl-copy."""
from __future__ import annotations

import os
import re
import shutil
from typing import Callable, Sequence

from kivo.core.bounded_process import BoundedProcessResult, run_bounded
from kivo.clipboard.models import (
    ClipType,
    MAX_IMAGE_CONTENT_BYTES,
    MAX_TEXT_CONTENT_BYTES,
    sanitize_preview,
)

_COMMAND_TIMEOUT = 1.0
_LIST_TIMEOUT = 2.0
_MAX_STDOUT_LIST = 256 * 1024
_MAX_STDERR = 4 * 1024
_ID_PATTERN = re.compile(r"^\d{1,12}$")
_BINARY_PREVIEW_PATTERN = re.compile(
    r"^\[\[\s*binary\s+data\s+([0-9.]+\s*[KMGT]?i?B)\s*([a-zA-Z0-9]+)?(?:\s+([0-9x]+))?\s*\]\]"
)
_UNSET = object()


class CliphistRunner:
    """Bounded, argv-only runner for cliphist and wl-copy."""

    def __init__(
        self,
        cliphist_path: str | None | object = _UNSET,
        wl_copy_path: str | None | object = _UNSET,
        *,
        db_path: str | None = None,
        runner: Callable[..., BoundedProcessResult] = run_bounded,
    ):
        self._runner = runner
        self._cliphist_path = self._resolve_path(cliphist_path, "cliphist")
        self._wl_copy_path = self._resolve_path(wl_copy_path, "wl-copy")
        self._db_path = db_path

    @staticmethod
    def _resolve_path(path_override: str | None | object, name: str) -> str | None:
        if path_override is _UNSET:
            candidate = shutil.which(name)
        elif path_override is None:
            candidate = None
        else:
            candidate = str(path_override)

        if not candidate:
            return None
        resolved = os.path.abspath(candidate)
        if os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return resolved
        return None

    @property
    def is_available(self) -> bool:
        return self._cliphist_path is not None and self._wl_copy_path is not None

    @property
    def cliphist_path(self) -> str | None:
        return self._cliphist_path

    @property
    def wl_copy_path(self) -> str | None:
        return self._wl_copy_path

    # ── History Listing ────────────────────────────────────────────────────

    def list_metadata(
        self, limit: int = 100
    ) -> list[tuple[str, str, str, str]]:
        """List raw metadata from cliphist.

        Returns:
            list of (raw_id, preview, clip_type, mime_hint)
        """
        if self._cliphist_path is None:
            return []

        argv = [self._cliphist_path]
        if self._db_path:
            argv.extend(["-db-path", self._db_path])
        argv.append("list")

        result = self._runner(
            argv,
            timeout=_LIST_TIMEOUT,
            stdout_limit=_MAX_STDOUT_LIST,
            stderr_limit=_MAX_STDERR,
        )

        if result.returncode != 0 or not result.stdout:
            return []

        items: list[tuple[str, str, str, str]] = []
        lines = result.stdout.splitlines()

        for line in lines:
            if len(items) >= limit:
                break
            line_str = line.strip()
            if not line_str or "\t" not in line:
                continue

            parts = line.split("\t", 1)
            raw_id = parts[0].strip()
            raw_preview = parts[1].strip() if len(parts) > 1 else ""

            if not _ID_PATTERN.match(raw_id):
                continue

            # Determine item type from preview pattern
            binary_match = _BINARY_PREVIEW_PATTERN.match(raw_preview)
            if binary_match:
                fmt = (binary_match.group(2) or "").lower()
                if fmt in ("png", "jpeg", "jpg", "webp", "bmp", "gif"):
                    clip_type = ClipType.IMAGE.value
                    mime_hint = f"image/{fmt}"
                else:
                    clip_type = ClipType.OTHER.value
                    mime_hint = "application/octet-stream"
                preview = raw_preview
            else:
                clip_type = ClipType.TEXT.value
                mime_hint = "text/plain"
                preview = sanitize_preview(raw_preview)

            items.append((raw_id, preview, clip_type, mime_hint))

        return items

    # ── Decode Item ────────────────────────────────────────────────────────

    def decode_item(self, raw_id: str | int) -> bytes | None:
        """Decode one explicitly requested item by ID."""
        if self._cliphist_path is None:
            return None

        id_str = str(raw_id).strip()
        if not _ID_PATTERN.match(id_str):
            return None

        argv = [self._cliphist_path]
        if self._db_path:
            argv.extend(["-db-path", self._db_path])
        argv.extend(["decode", id_str])

        result = self._runner(
            argv,
            timeout=_COMMAND_TIMEOUT,
            stdout_limit=MAX_IMAGE_CONTENT_BYTES,
            stderr_limit=_MAX_STDERR,
        )

        if result.returncode != 0:
            return None

        return result.stdout.encode("utf-8", errors="surrogateescape")

    # ── Copy Item ──────────────────────────────────────────────────────────

    def copy_item(
        self, data: bytes, mime_type: str = "text/plain"
    ) -> bool:
        """Copy data to Wayland clipboard using wl-copy."""
        if self._wl_copy_path is None or not data:
            return False

        mime = str(mime_type).strip() or "text/plain"
        argv = [self._wl_copy_path, "--type", mime]

        result = self._runner(
            argv,
            timeout=_COMMAND_TIMEOUT,
            input_data=data,
            stdout_limit=_MAX_STDERR,
            stderr_limit=_MAX_STDERR,
        )

        return result.returncode == 0 and not result.timed_out

    # ── Delete Item ────────────────────────────────────────────────────────

    def delete_item(self, raw_id: str | int) -> bool:
        """Delete one history item from cliphist."""
        if self._cliphist_path is None:
            return False

        id_str = str(raw_id).strip()
        if not _ID_PATTERN.match(id_str):
            return False

        argv = [self._cliphist_path]
        if self._db_path:
            argv.extend(["-db-path", self._db_path])
        argv.append("delete")

        result = self._runner(
            argv,
            timeout=_COMMAND_TIMEOUT,
            input_data=id_str,
            stdout_limit=_MAX_STDERR,
            stderr_limit=_MAX_STDERR,
        )

        return result.returncode == 0 and not result.timed_out

    # ── Wipe History ───────────────────────────────────────────────────────

    def wipe_history(self) -> bool:
        """Wipe entire history in cliphist (explicit user action only)."""
        if self._cliphist_path is None:
            return False

        argv = [self._cliphist_path]
        if self._db_path:
            argv.extend(["-db-path", self._db_path])
        argv.append("wipe")

        result = self._runner(
            argv,
            timeout=_COMMAND_TIMEOUT,
            stdout_limit=_MAX_STDERR,
            stderr_limit=_MAX_STDERR,
        )

        return result.returncode == 0 and not result.timed_out


__all__ = ["CliphistRunner"]
