"""Integrity checks for the shared Python/Kotlin download interoperability vectors."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import unittest
from pathlib import Path

from kivo.bridge.transfer_crypto import (
    build_canonical_transfer_request,
    build_canonical_transfer_response,
    compute_transfer_request_signature,
    compute_transfer_response_signature,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "download_interop_vectors.json"


class DownloadInteropFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text())
        cls.session = cls.fixture["session_id"]
        cls.k_up = bytes.fromhex(cls.fixture["upstream_key_hex"])
        cls.k_down = bytes.fromhex(cls.fixture["downstream_key_hex"])
        cls.data = bytes(i % 251 for i in range(cls.fixture["file_size"]))

    def test_request_vectors_recompute_exactly(self):
        for name, vector in self.fixture["requests"].items():
            body = vector["body_utf8"].encode("utf-8")
            self.assertEqual(len(body), vector["content_length"], name)
            canonical = build_canonical_transfer_request(
                version=self.fixture["protocol_version"],
                session_id=self.session,
                method=vector["method"],
                endpoint=vector["endpoint"],
                sequence=vector["sequence"],
                nonce=bytes.fromhex(vector["nonce_hex"]),
                timestamp=vector["timestamp"],
                content_length=vector["content_length"],
                content_sha256=bytes.fromhex(vector["content_sha256"]),
            )
            self.assertEqual(canonical.hex(), vector["canonical_hex"], name)
            self.assertEqual(
                compute_transfer_request_signature(self.k_up, canonical).hex(),
                vector["signature_hex"],
                name,
            )

    def test_response_vectors_recompute_exactly(self):
        for name, vector in self.fixture["responses"].items():
            payload_bytes = vector["payload_utf8"].encode("utf-8")
            payload_sha = hashlib.sha256(payload_bytes).digest()
            self.assertEqual(payload_sha.hex(), vector["payload_sha256"], name)
            canonical = build_canonical_transfer_response(
                version=self.fixture["protocol_version"],
                response_context=f"download_{vector['request']}",
                session_id=self.session,
                http_status=vector["http_status"],
                sequence=vector["sequence"],
                nonce=bytes.fromhex(vector["nonce_hex"]),
                state=vector["state"],
                transfer_id=vector["transfer_id"],
                payload_length=len(payload_bytes),
                payload_sha256=payload_sha,
            )
            self.assertEqual(canonical.hex(), vector["canonical_hex"], name)
            self.assertEqual(
                compute_transfer_response_signature(self.k_down, canonical).hex(),
                vector["signature_hex"],
                name,
            )
            envelope = vector["envelope"]
            self.assertEqual(
                base64.b64decode(envelope["payload_b64"]), payload_bytes, name
            )
            self.assertEqual(envelope["sig"], vector["signature_hex"], name)

    def test_chunk_payload_is_self_consistent(self):
        payload = json.loads(self.fixture["responses"]["chunk"]["payload_utf8"])
        chunk = base64.b64decode(payload["chunk_b64"])
        self.assertEqual(payload["chunk_index"], self.fixture["chunk_index"])
        self.assertEqual(payload["offset"], 0)
        self.assertEqual(payload["length"], len(chunk))
        self.assertEqual(hashlib.sha256(chunk).hexdigest(), payload["chunk_sha256"])
        self.assertEqual(chunk, self.data)
        self.assertEqual(
            hashlib.sha256(self.data).hexdigest(), self.fixture["sha256"]
        )

    def test_public_metadata_fields_are_path_free(self):
        for name in ("init", "status", "chunk", "complete", "cancel", "expired"):
            payload = json.loads(self.fixture["responses"][name]["payload_utf8"])
            for key in (
                "download_id",
                "device_id",
                "filename",
                "size",
                "sha256",
                "state",
                "bytes_sent",
                "next_chunk_index",
                "total_chunks",
                "chunk_size",
                "expires_at",
                "claim_expires_at",
                "active_expires_at",
            ):
                self.assertIn(key, payload, name)
            self.assertEqual(payload["download_id"], self.fixture["ticket_id"])
            self.assertEqual(payload["device_id"], self.fixture["device_id"])
        encoded = FIXTURE_PATH.read_text()
        self.assertNotIn("/home/", encoded)
        self.assertNotIn("/tmp/", encoded)
        self.assertNotIn("file:///", encoded)
        self.assertNotIn("bearer", encoded.lower())
        self.assertIsNone(re.search(r"Bearer\s", encoded))


if __name__ == "__main__":
    unittest.main()
