"""Unit and adversarial tests for transfer authentication transcripts and replay."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import unittest
from pathlib import Path

from kivo.bridge.transfer_crypto import (
    TRANSFER_NONCE_BYTES,
    TRANSFER_PROTOCOL_VERSION,
    TRANSFER_REQUEST_LABEL,
    TRANSFER_RESPONSE_LABEL,
    TRANSFER_RESPONSE_CONTEXTS,
    build_canonical_transfer_request,
    build_canonical_transfer_response,
    compute_transfer_request_signature,
    compute_transfer_response_signature,
    decode_nonce_b64,
    encode_nonce_b64,
    verify_transfer_request_signature,
    verify_transfer_response_signature,
)


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "transfer_test_vectors.json").read_text()
)
K_UP = bytes.fromhex(FIXTURES["upstream_key_hex"])
K_DOWN = bytes.fromhex(FIXTURES["downstream_key_hex"])


class TransferCryptoCanonicalTests(unittest.TestCase):

    def test_canonical_request_deterministic(self):
        nonce = os.urandom(16)
        sha256 = hashlib.sha256(b"body").digest()
        args = dict(
            version="1",
            session_id="sess_abc",
            method="POST",
            endpoint="/api/v1/transfers/uploads/init",
            sequence=1,
            nonce=nonce,
            timestamp=1700000000.0,
            content_length=100,
            content_sha256=sha256,
        )
        a = build_canonical_transfer_request(**args)
        b = build_canonical_transfer_request(**args)
        self.assertEqual(a, b)
        self.assertIsInstance(a, bytes)
        self.assertTrue(len(a) > 0)

    def test_canonical_request_field_boundaries(self):
        nonce = bytes(range(16))
        sha256 = bytes(32)
        canonical1 = build_canonical_transfer_request(
            version="1",
            session_id="sess_ab",
            method="POST",
            endpoint="/a",
            sequence=1,
            nonce=nonce,
            timestamp=1700000000.0,
            content_length=0,
            content_sha256=sha256,
        )
        canonical2 = build_canonical_transfer_request(
            version="1",
            session_id="sess_a",
            method="POST",
            endpoint="/ab",
            sequence=1,
            nonce=nonce,
            timestamp=1700000000.0,
            content_length=0,
            content_sha256=sha256,
        )
        self.assertNotEqual(canonical1, canonical2)

    def test_signature_roundtrip(self):
        nonce = os.urandom(16)
        sha256 = hashlib.sha256(b"test").digest()
        canonical = build_canonical_transfer_request(
            version="1",
            session_id="sess_test",
            method="POST",
            endpoint="/api/v1/transfers/uploads/init",
            sequence=42,
            nonce=nonce,
            timestamp=1700000000.0,
            content_length=4,
            content_sha256=sha256,
        )
        sig = compute_transfer_request_signature(K_UP, canonical)
        self.assertEqual(len(sig), 32)
        self.assertTrue(verify_transfer_request_signature(K_UP, canonical, sig))
        self.assertFalse(verify_transfer_request_signature(K_DOWN, canonical, sig))
        self.assertFalse(verify_transfer_request_signature(K_UP, canonical, b"\x00" * 32))

    def test_response_roundtrip(self):
        nonce = os.urandom(16)
        sha256 = os.urandom(32)
        canonical = build_canonical_transfer_response(
            version="1",
            response_context="upload_init",
            session_id="sess_test",
            http_status=202,
            sequence=1,
            nonce=nonce,
            state="awaiting_desktop_approval",
            transfer_id="tr_aabbccddee112233aabbccdd",
            payload_length=100,
            payload_sha256=sha256,
        )
        sig = compute_transfer_response_signature(K_DOWN, canonical)
        self.assertTrue(verify_transfer_response_signature(K_DOWN, canonical, sig))
        self.assertFalse(verify_transfer_response_signature(K_UP, canonical, sig))

    def test_response_contexts(self):
        for ctx in TRANSFER_RESPONSE_CONTEXTS:
            canonical = build_canonical_transfer_response(
                version="1",
                response_context=ctx,
                session_id="sess_x",
                http_status=200,
                sequence=1,
                nonce=bytes(16),
                state="ok",
                transfer_id="",
                payload_length=0,
                payload_sha256=bytes(32),
            )
            self.assertIsInstance(canonical, bytes)

    def test_invalid_response_context(self):
        with self.assertRaises(ValueError):
            build_canonical_transfer_response(
                version="1",
                response_context="invalid_context",
                session_id="sess_x",
                http_status=200,
                sequence=1,
                nonce=bytes(16),
                state="ok",
                transfer_id="",
                payload_length=0,
                payload_sha256=bytes(32),
            )

    def test_rejects_wrong_nonce_length(self):
        sha256 = bytes(32)
        with self.assertRaises(ValueError):
            build_canonical_transfer_request(
                version="1", session_id="s", method="POST", endpoint="/x",
                sequence=1, nonce=b"\x00" * 15, timestamp=1.0,
                content_length=0, content_sha256=sha256,
            )
        with self.assertRaises(ValueError):
            build_canonical_transfer_request(
                version="1", session_id="s", method="POST", endpoint="/x",
                sequence=1, nonce=b"\x00" * 17, timestamp=1.0,
                content_length=0, content_sha256=sha256,
            )

    def test_rejects_invalid_sequence(self):
        sha256 = bytes(32)
        nonce = bytes(16)
        for bad in (0, -1, 2**63):
            with self.assertRaises(ValueError, msg=f"seq={bad}"):
                build_canonical_transfer_request(
                    version="1", session_id="s", method="POST", endpoint="/x",
                    sequence=bad, nonce=nonce, timestamp=1.0,
                    content_length=0, content_sha256=sha256,
                )

    def test_rejects_bad_timestamp(self):
        sha256 = bytes(32)
        nonce = bytes(16)
        for bad in (float("inf"), float("-inf"), float("nan"), -1.0, 4102444801.0):
            with self.assertRaises(ValueError, msg=f"ts={bad}"):
                build_canonical_transfer_request(
                    version="1", session_id="s", method="POST", endpoint="/x",
                    sequence=1, nonce=nonce, timestamp=bad,
                    content_length=0, content_sha256=sha256,
                )

    def test_rejects_wrong_sha256_length(self):
        nonce = bytes(16)
        for bad_len in (0, 16, 31, 33, 64):
            with self.assertRaises(ValueError, msg=f"sha256_len={bad_len}"):
                build_canonical_transfer_request(
                    version="1", session_id="s", method="POST", endpoint="/x",
                    sequence=1, nonce=nonce, timestamp=1.0,
                    content_length=0, content_sha256=b"\x00" * bad_len,
                )


class NonceEncodingTests(unittest.TestCase):

    def test_roundtrip(self):
        for _ in range(10):
            nonce = os.urandom(TRANSFER_NONCE_BYTES)
            encoded = encode_nonce_b64(nonce)
            decoded = decode_nonce_b64(encoded)
            self.assertEqual(nonce, decoded)

    def test_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            decode_nonce_b64(encode_nonce_b64(os.urandom(16))[:-2])

    def test_rejects_padding(self):
        nonce = os.urandom(16)
        padded = base64.urlsafe_b64encode(nonce).decode("ascii")
        if padded.endswith("="):
            with self.assertRaises(ValueError):
                decode_nonce_b64(padded)

    def test_rejects_non_base64url(self):
        with self.assertRaises(ValueError):
            decode_nonce_b64("AAAA+AAABBBB/CCC")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            decode_nonce_b64("")

    def test_rejects_non_string(self):
        with self.assertRaises(ValueError):
            decode_nonce_b64(None)
        with self.assertRaises(ValueError):
            decode_nonce_b64(123)


class TransferLabelTests(unittest.TestCase):

    def test_labels_end_with_null(self):
        self.assertTrue(TRANSFER_REQUEST_LABEL.endswith(b"\x00"))
        self.assertTrue(TRANSFER_RESPONSE_LABEL.endswith(b"\x00"))

    def test_labels_differ(self):
        self.assertNotEqual(TRANSFER_REQUEST_LABEL, TRANSFER_RESPONSE_LABEL)

    def test_request_and_response_sigs_differ_for_same_key(self):
        key = os.urandom(32)
        data = b"same canonical data"
        req_sig = compute_transfer_request_signature(key, data)
        resp_sig = compute_transfer_response_signature(key, data)
        self.assertNotEqual(req_sig, resp_sig)


if __name__ == "__main__":
    unittest.main()
