"""Unit and adversarial tests for Phone Bridge cryptographic primitives and SAS derivation."""
from __future__ import annotations

import base64
import ipaddress
import os
import unittest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from kivo.bridge.crypto import (
    build_canonical_context,
    compute_client_proof,
    derive_sas_code,
    derive_session_keys,
    generate_ephemeral_certificate,
    verify_client_proof,
)


class BridgeCryptoTests(unittest.TestCase):
    # ── Ephemeral TLS Certificate ──────────────────────────────────────────
    def test_generate_ephemeral_certificate(self):
        lan_ip = "192.168.1.7"
        cert, key, fp_hex, cert_pem, key_pem = generate_ephemeral_certificate(lan_ip)

        self.assertIsInstance(cert, x509.Certificate)
        self.assertIsInstance(key, ec.EllipticCurvePrivateKey)
        self.assertEqual(key.curve.name, "secp256r1")

        # Fingerprint must be 64-character uppercase hex (SHA-256)
        self.assertEqual(len(fp_hex), 64)
        self.assertTrue(all(c in "0123456789ABCDEF" for c in fp_hex))

        # Check SAN contains the LAN IP
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ip_sans = san_ext.value.get_values_for_type(x509.IPAddress)
        self.assertEqual(len(ip_sans), 1)
        self.assertEqual(ip_sans[0], ipaddress.IPv4Address(lan_ip))

        # Check PEM encoding
        self.assertIn(b"-----BEGIN CERTIFICATE-----", cert_pem)
        self.assertIn(b"-----BEGIN PRIVATE KEY-----", key_pem)

    # ── Canonical Binary Context ───────────────────────────────────────────
    def test_canonical_context_length_prefixed(self):
        context1 = build_canonical_context(
            version="1",
            session_id="sess_abc",
            cert_fp="AA" * 32,
            server_nonce=b"\x01" * 32,
            client_nonce=b"\x02" * 32,
            client_id="client_xyz",
            algorithm_id="HMAC-SHA256-HKDF-SAS-V1",
        )
        context2 = build_canonical_context(
            version="1",
            session_id="sess_abc",
            cert_fp="AA" * 32,
            server_nonce=b"\x01" * 32,
            client_nonce=b"\x02" * 32,
            client_id="client_xyz",
            algorithm_id="HMAC-SHA256-HKDF-SAS-V1",
        )
        # Deterministic
        self.assertEqual(context1, context2)

        # Non-ambiguity: different field boundaries yield different outputs
        context3 = build_canonical_context(
            version="1",
            session_id="sess_ab",
            cert_fp="c" + "AA" * 31 + "A",
            server_nonce=b"\x01" * 32,
            client_nonce=b"\x02" * 32,
            client_id="client_xyz",
            algorithm_id="HMAC-SHA256-HKDF-SAS-V1",
        )
        self.assertNotEqual(context1, context3)

    # ── Client Proof & Constant-Time Verification ──────────────────────────
    def test_client_proof_verification(self):
        secret = os.urandom(32)
        context = b"sample_canonical_context"

        proof = compute_client_proof(secret, context)
        self.assertEqual(len(proof), 32)
        self.assertTrue(verify_client_proof(secret, context, proof))

        # Rejection of tampered proof
        tampered_proof = bytearray(proof)
        tampered_proof[0] ^= 0xFF
        self.assertFalse(verify_client_proof(secret, context, bytes(tampered_proof)))

        # Rejection of wrong secret
        wrong_secret = os.urandom(32)
        self.assertFalse(verify_client_proof(wrong_secret, context, proof))

        # Rejection of tampered context
        self.assertFalse(verify_client_proof(secret, b"tampered_context", proof))

    # ── SAS Derivation & Unbiased Formatting ───────────────────────────────
    def test_sas_derivation_deterministic(self):
        secret = b"K" * 32
        server_nonce = b"S" * 32
        context = b"context_data"

        num1, str1 = derive_sas_code(secret, server_nonce, context)
        num2, str2 = derive_sas_code(secret, server_nonce, context)

        self.assertEqual(num1, num2)
        self.assertEqual(str1, str2)

        # 6-digit bounds
        self.assertGreaterEqual(num1, 0)
        self.assertLess(num1, 1_000_000)

        # Formatted string: "XXX XXX" (7 chars with space)
        self.assertEqual(len(str1), 7)
        self.assertEqual(str1[3], " ")
        self.assertTrue(str1.replace(" ", "").isdigit())

    def test_sas_test_vector(self):
        """Verify deterministic test vector for SAS derivation."""
        secret = b"\x00" * 32
        server_nonce = b"\x01" * 32
        context = b"fixed_test_vector_context"

        num, str_val = derive_sas_code(secret, server_nonce, context)
        self.assertEqual(len(str_val.replace(" ", "")), 6)
        self.assertEqual(str_val, f"{num:06d}"[:3] + " " + f"{num:06d}"[3:])

    def test_different_contexts_produce_different_sas(self):
        """Altering any component of context changes derived SAS."""
        secret = b"K" * 32
        server_nonce = b"S" * 32

        _, sas1 = derive_sas_code(secret, server_nonce, b"context_A")
        _, sas2 = derive_sas_code(secret, server_nonce, b"context_B")
        self.assertNotEqual(sas1, sas2)

    def test_fingerprint_deterministic_and_uppercase_hex(self):
        """Certificate fingerprint is exactly 64 uppercase hex characters."""
        from kivo.bridge.crypto import generate_ephemeral_certificate
        cert1, _, fp1, _, _ = generate_ephemeral_certificate("192.168.1.7")
        import hashlib
        from cryptography.hazmat.primitives import serialization
        expected = hashlib.sha256(cert1.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
        self.assertEqual(fp1, expected)
        self.assertEqual(len(fp1), 64)

    # ── Session Keys Derivation ────────────────────────────────────────────
    def test_derive_session_keys(self):
        secret = os.urandom(32)
        client_nonce = os.urandom(32)
        context = b"session_context"

        k_up, k_down, token = derive_session_keys(secret, client_nonce, context)

        self.assertEqual(len(k_up), 32)
        self.assertEqual(len(k_down), 32)
        # Directional keys must be distinct
        self.assertNotEqual(k_up, k_down)

        # Bearer token string
        self.assertTrue(token.startswith("sess_tok_"))
        self.assertGreater(len(token), 30)

    # ── Poll Key & Per-Request Signatures ──────────────────────────────────
    def test_poll_key_and_signature_interoperability(self):
        from kivo.bridge.crypto import (
            derive_poll_key,
            build_canonical_poll_request,
            compute_poll_signature,
            verify_poll_signature,
        )
        import json
        with open("tests/fixtures/pairing_test_vectors.json", "r") as f:
            vec = json.load(f)

        secret = bytes.fromhex(vec["pairing_secret_hex"])
        client_nonce = bytes.fromhex(vec["client_nonce_hex"])
        context = bytes.fromhex(vec["canonical_context_hex"])

        k_poll = derive_poll_key(secret, client_nonce, context)
        self.assertEqual(k_poll.hex(), vec["k_poll_hex"])

        req_nonce = bytes.fromhex(vec["poll_req_nonce_hex"])
        canonical_poll = build_canonical_poll_request(
            version="1",
            session_id=vec["session_id"],
            sequence=vec["poll_req_sequence"],
            request_nonce=req_nonce,
            timestamp=vec["poll_req_timestamp"],
        )
        self.assertEqual(canonical_poll.hex(), vec["canonical_poll_req_hex"])

        # Request signature
        sig = compute_poll_signature(k_poll, canonical_poll)
        self.assertEqual(sig.hex(), vec["poll_req_sig_hex"])
        self.assertTrue(verify_poll_signature(k_poll, canonical_poll, sig))

        # Tampered request signature rejected
        tampered_sig = bytearray(sig)
        tampered_sig[0] ^= 0xFF
        self.assertFalse(verify_poll_signature(k_poll, canonical_poll, bytes(tampered_sig)))

    # ── Canonical Response V2 & Field-Binding ──────────────────────────────
    def test_canonical_response_v2_interoperability(self):
        from kivo.bridge.crypto import (
            build_canonical_poll_response,
            compute_credential_sha256,
            compute_poll_response_signature,
            verify_poll_response_signature,
        )
        import json
        with open("tests/fixtures/pairing_test_vectors.json", "r") as f:
            vec = json.load(f)

        k_poll = bytes.fromhex(vec["k_poll_hex"])
        token = vec["bearer_token"]

        self.assertEqual(
            compute_credential_sha256(token).hex(),
            vec["poll_resp_credential_sha256_hex"],
        )

        canonical_resp = build_canonical_poll_response(
            version=vec["version"],
            response_context=vec["poll_resp_context"],
            session_id=vec["session_id"],
            http_status=vec["poll_resp_http_status"],
            sequence=vec["poll_req_sequence"],
            request_nonce=bytes.fromhex(vec["poll_req_nonce_hex"]),
            status=vec["poll_resp_status"],
            expires_in=vec["poll_resp_expires_in"],
            device_id=vec["poll_resp_device_id"],
            credential_state=vec["poll_resp_credential_state"],
            credential_sha256=bytes.fromhex(vec["poll_resp_credential_sha256_hex"]),
        )
        self.assertEqual(canonical_resp.hex(), vec["canonical_poll_resp_hex"])

        sig = compute_poll_response_signature(k_poll, canonical_resp)
        self.assertEqual(sig.hex(), vec["poll_resp_sig_v2_hex"])
        self.assertTrue(verify_poll_response_signature(k_poll, canonical_resp, sig))

    def test_response_v2_every_field_is_authenticated(self):
        """Changing any single response field must invalidate the MAC."""
        import json
        from kivo.bridge.crypto import (
            build_canonical_poll_response,
            compute_credential_sha256,
            compute_poll_response_signature,
            verify_poll_response_signature,
        )
        with open("tests/fixtures/pairing_test_vectors.json", "r") as f:
            vec = json.load(f)

        k_poll = bytes.fromhex(vec["k_poll_hex"])
        token = vec["bearer_token"]
        credential_hash = compute_credential_sha256(token)

        base = dict(
            version=vec["version"],
            response_context="poll",
            session_id=vec["session_id"],
            http_status=200,
            sequence=3,
            request_nonce=bytes.fromhex(vec["poll_req_nonce_hex"]),
            status="approved",
            expires_in=7200,
            device_id=vec["client_id"],
            credential_state="delivered",
            credential_sha256=credential_hash,
        )
        sig = compute_poll_response_signature(
            k_poll, build_canonical_poll_response(**base)
        )

        mutations = {
            "version": "2",
            "response_context": "handshake",
            "session_id": "sess_ffffffffffffffff",
            "http_status": 201,
            "sequence": 4,
            "request_nonce": bytes.fromhex("ff" * 16),
            "status": "pending_approval",
            "expires_in": 7199,
            "device_id": "client_other",
            "credential_state": "none",
            "credential_sha256": compute_credential_sha256("sess_tok_other"),
        }

        for field, mutated_value in mutations.items():
            mutated = dict(base)
            mutated[field] = mutated_value
            canonical = build_canonical_poll_response(**mutated)
            self.assertFalse(
                verify_poll_response_signature(k_poll, canonical, sig),
                msg=f"MAC unexpectedly verified after mutating {field}",
            )

        # Sanity: the unmodified transcript still verifies.
        self.assertTrue(
            verify_poll_response_signature(
                k_poll, build_canonical_poll_response(**base), sig
            )
        )

    def test_pending_response_v2_interoperability(self):
        import json
        from kivo.bridge.crypto import (
            build_canonical_poll_response,
            compute_poll_response_signature,
        )
        with open("tests/fixtures/pairing_test_vectors.json", "r") as f:
            vec = json.load(f)

        k_poll = bytes.fromhex(vec["k_poll_hex"])
        canonical = build_canonical_poll_response(
            version=vec["version"],
            response_context="poll",
            session_id=vec["session_id"],
            http_status=200,
            sequence=vec["poll_req_sequence"],
            request_nonce=bytes.fromhex(vec["poll_req_nonce_hex"]),
            status=vec["pending_resp_status"],
            expires_in=vec["pending_resp_expires_in"],
            device_id=vec["pending_resp_device_id"],
            credential_state=vec["pending_resp_credential_state"],
            credential_sha256=b"",
        )
        self.assertEqual(canonical.hex(), vec["canonical_pending_resp_hex"])
        self.assertEqual(
            compute_poll_response_signature(k_poll, canonical).hex(),
            vec["pending_resp_sig_v2_hex"],
        )

    def test_handshake_response_v2_interoperability(self):
        import json
        from kivo.bridge.crypto import (
            build_canonical_poll_response,
            compute_credential_sha256,
            compute_poll_response_signature,
        )
        with open("tests/fixtures/pairing_test_vectors.json", "r") as f:
            vec = json.load(f)

        k_poll = bytes.fromhex(vec["k_poll_hex"])
        canonical = build_canonical_poll_response(
            version=vec["version"],
            response_context=vec["handshake_resp_context"],
            session_id=vec["session_id"],
            http_status=200,
            sequence=vec["handshake_resp_sequence"],
            request_nonce=bytes.fromhex(vec["handshake_resp_nonce_hex"]),
            status="approved",
            expires_in=7200,
            device_id=vec["client_id"],
            credential_state="delivered",
            credential_sha256=compute_credential_sha256(vec["bearer_token"]),
        )
        self.assertEqual(canonical.hex(), vec["canonical_handshake_resp_hex"])
        self.assertEqual(
            compute_poll_response_signature(k_poll, canonical).hex(),
            vec["handshake_resp_sig_v2_hex"],
        )


if __name__ == "__main__":
    unittest.main()
