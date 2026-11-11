"""Deterministic download fixture integrity tests (synthetic bytes only)."""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

from kivo.bridge.download_validation import DOWNLOAD_CHUNK_BYTES, chunk_length, total_chunks

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "download_test_vectors.json"


class DownloadFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text())
        size = cls.fixture["file_size"]
        cls.data = bytes(index % 251 for index in range(size))

    def test_fixture_geometry_and_digests_are_self_consistent(self):
        size = self.fixture["file_size"]
        self.assertEqual(self.fixture["chunk_bytes"], DOWNLOAD_CHUNK_BYTES)
        self.assertEqual(total_chunks(size), self.fixture["total_chunks"])
        self.assertEqual(len(self.fixture["chunk_sha256"]), self.fixture["total_chunks"])
        self.assertEqual(hashlib.sha256(self.data).hexdigest(), self.fixture["sha256"])
        self.assertEqual(hashlib.sha256(b"").hexdigest(), self.fixture["empty_sha256"])

        offset = 0
        for index, expected in enumerate(self.fixture["chunk_sha256"]):
            length = chunk_length(size, index)
            digest = hashlib.sha256(self.data[offset : offset + length]).hexdigest()
            self.assertEqual(digest, expected, f"chunk {index} digest drifted")
            offset += length
        self.assertEqual(offset, size)

    def test_fixture_identifiers_and_routes(self):
        self.assertRegex(self.fixture["download_id"], r"^tkt_[0-9a-f]{64}$")
        self.assertRegex(self.fixture["shelf_item_id"], r"^shelf_[0-9a-f]{12}$")
        self.assertRegex(self.fixture["session_id"], r"^sess_[A-Za-z0-9_.-]+$")
        endpoints = self.fixture["endpoints"]
        for name in ("init", "status", "chunk", "complete", "cancel"):
            self.assertTrue(endpoints[name].startswith("/api/v1/transfers/downloads/"))
        self.assertEqual(
            self.fixture["states"], ["issued", "claimed", "streaming", "completed"]
        )
        self.assertEqual(
            set(self.fixture["terminal_states"]),
            {"expired", "revoked", "failed", "cancelled"},
        )

    def test_fixture_contains_no_paths_or_secrets(self):
        encoded = FIXTURE_PATH.read_text()
        self.assertNotIn("/home/", encoded)
        self.assertNotIn("/tmp/", encoded)
        self.assertNotIn("bearer", encoded.lower())
        self.assertNotIn("key_hex", encoded.lower())
        self.assertNotIn("BEGIN ", encoded)
        self.assertIsNone(re.search(r"file:///", encoded))


if __name__ == "__main__":
    unittest.main()
