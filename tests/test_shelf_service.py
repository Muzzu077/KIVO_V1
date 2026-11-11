"""Unit and adversarial tests for FileShelfService."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from kivo.core.bounded_process import BoundedProcessResult
from kivo.shelf.models import MAX_BATCH_URIS, MAX_SHELF_ITEMS, ShelfItem
from kivo.shelf.path_policy import PathAuthorizer, ShelfPolicyError
from kivo.shelf.service import FileShelfService


class FileShelfServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.files_dir = self.root / "files"
        self.files_dir.mkdir(parents=True)

        self.authorizer = PathAuthorizer()
        self.opened_commands: list[list[str]] = []

        def mock_runner(argv, **kw):
            self.opened_commands.append(list(argv))
            return BoundedProcessResult(returncode=0, stdout="", stderr="")

        self.service = FileShelfService(
            authorizer=self.authorizer,
            xdg_open_path="/usr/bin/xdg-open",
            runner=mock_runner,
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    # ── Adding Items ───────────────────────────────────────────────────────
    def test_add_valid_files(self):
        f1 = self.files_dir / "doc1.pdf"
        f1.write_bytes(b"content 1")
        f2 = self.files_dir / "photo.png"
        f2.write_bytes(b"content 2")

        added, rejected = self.service.add_items([
            f1.as_uri(),
            f2.as_uri(),
        ])
        self.assertEqual(len(added), 2)
        self.assertEqual(len(rejected), 0)
        self.assertEqual(added[0].name, "doc1.pdf")
        self.assertEqual(added[0].file_type, "pdf")
        self.assertEqual(added[1].name, "photo.png")

    def test_partial_batch_failure(self):
        valid_f = self.files_dir / "valid.txt"
        valid_f.write_text("ok")

        added, rejected = self.service.add_items([
            valid_f.as_uri(),
            "file:///nonexistent/missing.pdf",
            "http://evil.com/exploit.sh",
        ])
        self.assertEqual(len(added), 1)
        self.assertEqual(len(rejected), 2)
        self.assertEqual(added[0].name, "valid.txt")
        self.assertEqual(rejected[0]["code"], "file_not_found")
        self.assertEqual(rejected[1]["code"], "unsupported_scheme")

    def test_deduplication(self):
        f = self.files_dir / "shared.txt"
        f.write_text("shared")

        added1, _ = self.service.add_items([f.as_uri()])
        added2, _ = self.service.add_items([f.as_uri()])
        self.assertEqual(len(added1), 1)
        self.assertEqual(len(added2), 1)
        # Returns the same item reference, does not create duplicate in list
        self.assertEqual(added1[0].id, added2[0].id)
        items = self.service.list_items()
        self.assertEqual(len(items), 1)

    def test_batch_limit_enforced(self):
        uris = [f"file:///home/user/file_{i}.txt" for i in range(MAX_BATCH_URIS + 5)]
        with self.assertRaises(ShelfPolicyError) as raised:
            self.service.add_items(uris)
        self.assertEqual(raised.exception.code, "batch_too_large")

    def test_shelf_capacity_limit(self):
        # Create small shelf service
        files = []
        for i in range(MAX_SHELF_ITEMS + 2):
            p = self.files_dir / f"f_{i}.txt"
            p.write_text("x")
            files.append(p.as_uri())

        # Add in two batches of 20
        self.service.add_items(files[:20])
        self.service.add_items(files[20:40])
        added3, rejected3 = self.service.add_items(files[40:])
        self.assertEqual(len(self.service.list_items()), MAX_SHELF_ITEMS)
        self.assertTrue(any(r["code"] == "shelf_full" for r in rejected3))

    # ── List Items & Sanitization ──────────────────────────────────────────
    def test_list_items_excludes_absolute_paths_and_inodes(self):
        f = self.files_dir / "demo_file.txt"
        f.write_text("hello")
        added, _ = self.service.add_items([f.as_uri()])
        serialized = added[0].to_dict()

        # Invariant: absolute path and internal identities are NOT in to_dict()
        self.assertNotIn("canonical_path", serialized)
        self.assertNotIn("_canonical_path", serialized)
        self.assertNotIn("ino", serialized)
        self.assertNotIn("dev", serialized)
        self.assertNotIn(str(f), str(serialized))

    def test_stale_detection_on_deleted_file(self):
        f = self.files_dir / "ephemeral.txt"
        f.write_text("bye")
        added, _ = self.service.add_items([f.as_uri()])
        item_id = added[0].id

        # Delete from disk
        f.unlink()

        # Listing triggers freshness re-check
        items = self.service.list_items()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].stale)
        self.assertEqual(items[0].availability, "unavailable")

    # ── Remove & Clear ─────────────────────────────────────────────────────
    def test_remove_item_preserves_disk_file(self):
        f = self.files_dir / "keep_me.txt"
        f.write_text("important")
        added, _ = self.service.add_items([f.as_uri()])
        item_id = added[0].id

        removed = self.service.remove_item(item_id)
        self.assertTrue(removed)
        self.assertEqual(len(self.service.list_items()), 0)

        # Invariant: underlying file on disk was NOT deleted
        self.assertTrue(f.exists())
        self.assertEqual(f.read_text(), "important")

    def test_clear_preserves_disk_files(self):
        f1 = self.files_dir / "doc1.txt"
        f1.write_text("a")
        f2 = self.files_dir / "doc2.txt"
        f2.write_text("b")
        self.service.add_items([f1.as_uri(), f2.as_uri()])
        self.assertEqual(len(self.service.list_items()), 2)

        self.service.clear()
        self.assertEqual(len(self.service.list_items()), 0)
        # Disk files untouched
        self.assertTrue(f1.exists())
        self.assertTrue(f2.exists())

    # ── Open Item ──────────────────────────────────────────────────────────
    def test_open_item_launches_xdg_open(self):
        f = self.files_dir / "view.pdf"
        f.write_bytes(b"%PDF content")
        added, _ = self.service.add_items([f.as_uri()])
        item_id = added[0].id

        ok, code, msg = self.service.open_item(item_id)
        self.assertTrue(ok)
        self.assertIsNone(code)
        self.assertEqual(len(self.opened_commands), 1)
        self.assertEqual(self.opened_commands[0], ["/usr/bin/xdg-open", str(f)])

    def test_open_item_rejects_replaced_file(self):
        f = self.files_dir / "replace_me.txt"
        f.write_text("v1")
        added, _ = self.service.add_items([f.as_uri()])
        item_id = added[0].id

        # Replace file on disk
        f.unlink()
        f.write_text("v2")

        ok, code, msg = self.service.open_item(item_id)
        self.assertFalse(ok)
        self.assertEqual(code, "stale_reference")
        # xdg-open was NOT called for replaced file
        self.assertEqual(len(self.opened_commands), 0)


if __name__ == "__main__":
    unittest.main()
