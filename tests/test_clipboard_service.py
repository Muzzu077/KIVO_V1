"""Unit and adversarial tests for CliphistRunner, PinStore, and ClipboardService."""
from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kivo.clipboard.models import (
    ClipItem,
    ClipType,
    MAX_HISTORY_ITEMS,
    PrivacyLevel,
)
from kivo.clipboard.pin_store import PinStore
from kivo.clipboard.privacy import ClipboardPrivacyFilter
from kivo.clipboard.runner import CliphistRunner
from kivo.clipboard.service import ClipboardService
from kivo.core.bounded_process import BoundedProcessResult


class _FakeCliphistRunner(CliphistRunner):
    """In-memory mock for CliphistRunner that avoids touching host tools."""

    def __init__(self, initial_items: list[tuple[str, str, str, str]] | None = None):
        super().__init__(cliphist_path="/usr/bin/cliphist", wl_copy_path="/usr/bin/wl-copy")
        # raw_id -> (preview, clip_type, mime, content_bytes)
        self.storage: dict[str, tuple[str, str, str, bytes]] = {}
        if initial_items:
            for r_id, prev, c_type, mime in initial_items:
                self.storage[r_id] = (prev, c_type, mime, prev.encode("utf-8"))

        self.copied_data: list[tuple[bytes, str]] = []
        self.deleted_ids: list[str] = []
        self.wipe_called = False

    def list_metadata(self, limit: int = 100) -> list[tuple[str, str, str, str]]:
        res = []
        for r_id, (prev, c_type, mime, _) in list(self.storage.items())[:limit]:
            res.append((r_id, prev, c_type, mime))
        return res

    def decode_item(self, raw_id: str | int) -> bytes | None:
        entry = self.storage.get(str(raw_id))
        return entry[3] if entry else None

    def copy_item(self, data: bytes, mime_type: str = "text/plain") -> bool:
        self.copied_data.append((data, mime_type))
        return True

    def delete_item(self, raw_id: str | int) -> bool:
        r_str = str(raw_id)
        self.deleted_ids.append(r_str)
        if r_str in self.storage:
            del self.storage[r_str]
        return True

    def wipe_history(self) -> bool:
        self.wipe_called = True
        self.storage.clear()
        return True


class PinStoreTests(unittest.TestCase):
    def test_pin_store_atomic_save_and_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            pin_file = Path(tmp) / "kivo" / "clipboard_pins.json"
            store = PinStore(pin_file)
            item = ClipItem(id="clip_1", raw_id="1", preview="Pinned Note")
            self.assertTrue(store.add_pin(item))
            self.assertTrue(pin_file.exists())

            # Verify mode 0600 on file
            mode = stat.S_IMODE(pin_file.stat().st_mode)
            self.assertEqual(mode, 0o600)

            # Reload
            reloaded = PinStore(pin_file)
            pins = reloaded.get_all()
            self.assertEqual(len(pins), 1)
            self.assertEqual(pins[0].id, "clip_1")
            self.assertEqual(pins[0].preview, "Pinned Note")
            self.assertTrue(pins[0].is_pinned)

    def test_pin_store_rejects_sensitive_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            pin_file = Path(tmp) / "kivo" / "clipboard_pins.json"
            store = PinStore(pin_file)
            sensitive = ClipItem(
                id="clip_secret",
                raw_id="2",
                preview="••••••••••••",
                is_sensitive=True,
            )
            with self.assertRaises(ValueError):
                store.add_pin(sensitive)
            self.assertFalse(store.is_pinned("clip_secret"))

    def test_pin_store_handles_corrupt_file_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            pin_file = Path(tmp) / "kivo" / "clipboard_pins.json"
            pin_file.parent.mkdir(parents=True, exist_ok=True)
            pin_file.write_text("NOT_JSON_DATA_GARBAGE!!!")
            store = PinStore(pin_file)
            pins = store.get_all()
            self.assertEqual(pins, [])  # Fails safely with empty pins


class ClipboardServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "db"
        self.db_path.write_bytes(b"initial db")
        self.pin_path = Path(self.tmp.name) / "pins.json"

        self.initial_items = [
            ("101", "first normal text snippet", "text", "text/plain"),
            ("102", "ghp_1234567890abcdefghijklmnopqrstuvwxyz", "text", "text/plain"),
            ("103", "[[ binary data 500 KiB png 800x600 ]]", "image", "image/png"),
            ("104", "python function def run(): pass", "text", "text/plain"),
        ]
        self.runner = _FakeCliphistRunner(self.initial_items)
        self.pin_store = PinStore(self.pin_path)
        self.service = ClipboardService(
            runner=self.runner,
            pin_store=self.pin_store,
            db_path=self.db_path,
            poll_interval=0.1,
        )

    def tearDown(self) -> None:
        self.service.close(1.0)
        self.tmp.cleanup()

    # ── Refresh and Sensitive Masking ──────────────────────────────────────
    def test_refresh_masks_sensitive_items(self):
        items = self.service.refresh()
        self.assertEqual(len(items), 4)

        secret_item = next((it for it in items if it.raw_id == "102"), None)
        self.assertIsNotNone(secret_item)
        self.assertTrue(secret_item.is_sensitive)
        self.assertEqual(secret_item.preview, "••••••••••••")

        normal_item = next((it for it in items if it.raw_id == "101"), None)
        self.assertIsNotNone(normal_item)
        self.assertFalse(normal_item.is_sensitive)
        self.assertEqual(normal_item.preview, "first normal text snippet")

    # ── Search & Filter ────────────────────────────────────────────────────
    def test_history_search(self):
        self.service.refresh()
        results = self.service.get_history(search="python")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].raw_id, "104")

    def test_history_type_filter(self):
        self.service.refresh()
        images = self.service.get_history(filter_type="image")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].type, ClipType.IMAGE.value)

        texts = self.service.get_history(filter_type="text")
        self.assertEqual(len(texts), 3)

    # ── Get Detailed Item ──────────────────────────────────────────────────
    def test_get_item_sensitive_content_never_exposed(self):
        self.service.refresh()
        detail = self.service.get_item("clip_102")
        self.assertIsNotNone(detail)
        # Text content must be masked even in detailed get
        self.assertEqual(detail.to_dict()["text_content"], "••••••••••••")

    def test_get_item_normal_decodes(self):
        self.service.refresh()
        detail = self.service.get_item("clip_101")
        self.assertIsNotNone(detail)
        self.assertEqual(detail.text_content, "first normal text snippet")

    # ── Copy Item ──────────────────────────────────────────────────────────
    def test_copy_item_invokes_wl_copy(self):
        self.service.refresh()
        ok = self.service.copy_item("clip_101")
        self.assertTrue(ok)
        self.assertEqual(len(self.runner.copied_data), 1)
        self.assertEqual(self.runner.copied_data[0][0], b"first normal text snippet")
        self.assertEqual(self.runner.copied_data[0][1], "text/plain")

    # ── Pin and Unpin ──────────────────────────────────────────────────────
    def test_pin_and_unpin(self):
        self.service.refresh()
        # Pin item 101
        ok = self.service.pin_item("clip_101", True)
        self.assertTrue(ok)
        self.assertTrue(self.pin_store.is_pinned("clip_101"))

        # Pinned filter
        pinned = self.service.get_history(filter_type="pinned")
        self.assertEqual(len(pinned), 1)
        self.assertEqual(pinned[0].id, "clip_101")

        # Unpin
        ok2 = self.service.pin_item("clip_101", False)
        self.assertTrue(ok2)
        self.assertFalse(self.pin_store.is_pinned("clip_101"))

    def test_pin_sensitive_rejected(self):
        self.service.refresh()
        with self.assertRaises(ValueError):
            self.service.pin_item("clip_102", True)

    # ── Delete and Clear ───────────────────────────────────────────────────
    def test_delete_item(self):
        self.service.refresh()
        ok = self.service.delete_item("clip_101")
        self.assertTrue(ok)
        self.assertIn("101", self.runner.deleted_ids)
        items = self.service.get_history()
        self.assertNotIn("clip_101", [it.id for it in items])

    def test_clear_history(self):
        self.service.refresh()
        ok = self.service.clear_history()
        self.assertTrue(ok)
        self.assertTrue(self.runner.wipe_called)
        items = self.service.get_history()
        self.assertEqual(len(items), 0)

    # ── Lifecycle & Thread ─────────────────────────────────────────────────
    def test_service_start_and_close(self):
        before = threading.active_count()
        self.assertTrue(self.service.start())
        self.assertTrue(self.service.is_running)
        self.assertEqual(self.service.thread_name, "kivo-clipboard")
        time.sleep(0.1)
        self.service.close(1.0)
        self.assertFalse(self.service.is_running)
        time.sleep(0.1)
        after = threading.active_count()
        self.assertLessEqual(after, before)


if __name__ == "__main__":
    unittest.main()
