"""Clipboard intelligence service for KIVO.

Coordinates cliphist runner, privacy filtering, persistent pins, in-memory
caching, and background database modification detection.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from kivo.clipboard.models import (
    ClipDetail,
    ClipItem,
    ClipType,
    MAX_HISTORY_ITEMS,
    MAX_TEXT_CONTENT_BYTES,
    PrivacyLevel,
)
from kivo.clipboard.pin_store import PinStore
from kivo.clipboard.privacy import ClipboardPrivacyFilter
from kivo.clipboard.runner import CliphistRunner


def _default_db_path() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME")
    if cache and cache.strip():
        base = Path(cache)
    else:
        base = Path.home() / ".cache"
    return base / "cliphist" / "db"


class ClipboardService:
    """Manages clipboard history, search, pinning, and privacy masking."""

    def __init__(
        self,
        on_history_changed: Callable[[list[ClipItem]], None] | None = None,
        *,
        runner: CliphistRunner | None = None,
        privacy_filter: ClipboardPrivacyFilter | None = None,
        pin_store: PinStore | None = None,
        db_path: Path | str | None = None,
        poll_interval: float = 2.0,
    ):
        self._on_history_changed = on_history_changed
        self._db_path = Path(db_path) if db_path is not None else _default_db_path()
        self._runner = runner if runner is not None else CliphistRunner(db_path=str(self._db_path))
        self._privacy = privacy_filter if privacy_filter is not None else ClipboardPrivacyFilter()
        self._pin_store = pin_store if pin_store is not None else PinStore()
        self._poll_interval = float(poll_interval)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._items: list[ClipItem] = []
        self._last_mtime_ns: int = 0
        self._started = False
        self._running = False
        self._closed = False
        self._sequence = 0

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(self._running and thread is not None and thread.is_alive())

    @property
    def thread_name(self) -> str | None:
        thread = self._thread
        return thread.name if thread is not None else None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the background database monitor thread."""
        if self._closed:
            return False
        if self._running:
            return True

        self._started = True
        self._running = True
        self._stop_event.clear()

        # Prime the cache with an initial read
        self.refresh()

        self._thread = threading.Thread(
            target=self._thread_main,
            name="kivo-clipboard",
            daemon=True,
        )
        self._thread.start()
        return True

    def close(self, timeout: float = 2.0) -> None:
        """Stop background monitor thread and close the service."""
        if self._closed:
            return
        self._closed = True
        self._running = False
        self._stop_event.set()

        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and threading.current_thread() is not thread
        ):
            thread.join(timeout=max(0.1, float(timeout)))

    # ── Cache Refresh ──────────────────────────────────────────────────────

    def refresh(self) -> list[ClipItem]:
        """Fetch metadata from cliphist, evaluate privacy, and update cache."""
        raw_items = self._runner.list_metadata(limit=MAX_HISTORY_ITEMS)
        pinned_map = {p.id: p for p in self._pin_store.get_all()}

        new_items: list[ClipItem] = []
        seen_ids: set[str] = set()

        # 1. Process items from cliphist
        for raw_id, raw_preview, clip_type, mime_hint in raw_items:
            item_id = f"clip_{raw_id}"
            seen_ids.add(item_id)

            privacy_level, is_sensitive = self._privacy.evaluate(
                raw_preview, [mime_hint]
            )

            preview = (
                self._privacy.mask_sensitive_preview(raw_preview)
                if is_sensitive
                else raw_preview
            )

            is_pinned = item_id in pinned_map

            item = ClipItem(
                id=item_id,
                raw_id=raw_id,
                type=clip_type,
                preview=preview,
                char_count=len(raw_preview),
                size_bytes=len(raw_preview.encode("utf-8")),
                is_pinned=is_pinned,
                is_sensitive=is_sensitive,
                privacy_level=privacy_level.value,
                mime_type=mime_hint,
            )
            new_items.append(item)

        # 2. Add pinned items that may have scrolled out of cliphist history
        for p_id, p_item in pinned_map.items():
            if p_id not in seen_ids and len(new_items) < MAX_HISTORY_ITEMS:
                new_items.append(p_item)

        with self._lock:
            self._items = new_items
            self._sequence += 1
            current_list = list(self._items)

        self._notify_changed(current_list)
        return current_list

    def _notify_changed(self, items: list[ClipItem]) -> None:
        callback = self._on_history_changed
        if callback is not None and not self._closed:
            try:
                callback(items)
            except Exception:
                pass

    # ── Background Watcher Loop ────────────────────────────────────────────

    def _thread_main(self) -> None:
        # Record initial mtime
        try:
            if self._db_path.exists():
                self._last_mtime_ns = self._db_path.stat().st_mtime_ns
        except OSError:
            self._last_mtime_ns = 0

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._poll_interval)
            if self._stop_event.is_set():
                break

            try:
                if self._db_path.exists():
                    current_mtime = self._db_path.stat().st_mtime_ns
                    if current_mtime > self._last_mtime_ns:
                        self._last_mtime_ns = current_mtime
                        self.refresh()
            except OSError:
                pass

    # ── Public Queries & Operations ────────────────────────────────────────

    def get_history(
        self,
        limit: int = 50,
        search: str = "",
        filter_type: str = "all",
    ) -> list[ClipItem]:
        """Query in-memory clipboard items with filtering and search."""
        with self._lock:
            snapshot = list(self._items)

        search_term = (search or "").strip().lower()
        ftype = (filter_type or "all").strip().lower()

        filtered: list[ClipItem] = []
        for item in snapshot:
            # Type filter
            if ftype == "text" and item.type != ClipType.TEXT.value:
                continue
            if ftype == "image" and item.type != ClipType.IMAGE.value:
                continue
            if ftype == "pinned" and not item.is_pinned:
                continue

            # Substring search over preview
            if search_term and search_term not in item.preview.lower():
                continue

            filtered.append(item)
            if len(filtered) >= limit:
                break

        return filtered

    def get_item(self, item_id: str) -> ClipDetail | None:
        """Retrieve full details of one specific clipboard item."""
        with self._lock:
            target = next((it for it in self._items if it.id == item_id), None)

        if target is None:
            return None

        # Sensitive items never expose decoded text content
        if target.is_sensitive:
            return ClipDetail(
                item=target,
                text_content="••••••••••••",
                has_binary_data=False,
            )

        raw_bytes = self._runner.decode_item(target.raw_id)
        if raw_bytes is None:
            return ClipDetail(item=target, text_content="", has_binary_data=False)

        if target.type == ClipType.IMAGE.value:
            return ClipDetail(item=target, text_content=None, has_binary_data=True)

        try:
            text = raw_bytes[:MAX_TEXT_CONTENT_BYTES].decode("utf-8", errors="replace")
        except Exception:
            text = ""

        return ClipDetail(item=target, text_content=text, has_binary_data=False)

    def copy_item(self, item_id: str) -> bool:
        """Re-copy a previously recorded item to the system clipboard."""
        with self._lock:
            target = next((it for it in self._items if it.id == item_id), None)

        if target is None:
            return False

        raw_bytes = self._runner.decode_item(target.raw_id)
        if raw_bytes is None:
            return False

        success = self._runner.copy_item(raw_bytes, target.mime_type)
        if success:
            # Update cache after successful copy
            self.refresh()
        return success

    def pin_item(self, item_id: str, pin: bool = True) -> bool:
        """Pin or unpin an item. Rejects sensitive entries."""
        with self._lock:
            target = next((it for it in self._items if it.id == item_id), None)

        if target is None and pin:
            return False

        if pin:
            if target.is_sensitive:
                raise ValueError("Cannot pin sensitive clipboard item")
            ok = self._pin_store.add_pin(target)
        else:
            ok = self._pin_store.remove_pin(item_id)

        if ok:
            self.refresh()
        return ok

    def delete_item(self, item_id: str) -> bool:
        """Delete an item from both cliphist and KIVO pins."""
        with self._lock:
            target = next((it for it in self._items if it.id == item_id), None)

        raw_id = target.raw_id if target else item_id.replace("clip_", "")
        ok = self._runner.delete_item(raw_id)
        self._pin_store.remove_pin(item_id)
        self.refresh()
        return ok

    def clear_history(self) -> bool:
        """Clear all unpinned clipboard history (explicit user action)."""
        ok = self._runner.wipe_history()
        self.refresh()
        return ok


__all__ = ["ClipboardService"]
