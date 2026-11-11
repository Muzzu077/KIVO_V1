"""Unit and adversarial tests for PairingManager state machine."""
from __future__ import annotations

import base64
import os
import time
import unittest

from kivo.bridge.crypto import (
    build_canonical_context,
    build_canonical_poll_request,
    build_canonical_poll_response,
    compute_client_proof,
    compute_credential_sha256,
    compute_poll_response_signature,
    compute_poll_signature,
    derive_poll_key,
    verify_poll_response_signature,
)
from kivo.bridge.models import (
    ALGORITHM_ID,
    PAIRING_PROTOCOL_VERSION,
    BridgeState,
)
from kivo.bridge.pairing import PairingManager


class PairingManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pm = PairingManager()
        self.lan_ip = "192.168.1.7"
        self.port = 8443
        self.cert_fp = "A" * 64

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _handshake(self, *, approve: bool = False):
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_device_123"
        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=offer.session_id,
            cert_fp=offer.cert_fingerprint,
            server_nonce=offer.server_nonce,
            client_nonce=client_nonce,
            client_id=client_id,
            algorithm_id=ALGORITHM_ID,
        )
        proof = compute_client_proof(offer.pairing_secret, context)
        ok, err, _ = self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id=client_id,
            device_name="Pixel 8 Pro",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=proof,
        )
        self.assertTrue(ok)
        self.assertIsNone(err)
        k_poll = derive_poll_key(offer.pairing_secret, client_nonce, context)
        if approve:
            approved, _ = self.pm.approve_pairing(offer.session_id)
            self.assertTrue(approved)
        return offer, client_nonce, context, k_poll

    def _poll(
        self,
        session_id: str,
        k_poll: bytes,
        sequence: int,
        *,
        nonce: bytes | None = None,
        timestamp: float | None = None,
    ):
        request_nonce = os.urandom(16) if nonce is None else nonce
        ts = time.time() if timestamp is None else timestamp
        canonical = build_canonical_poll_request(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=session_id,
            sequence=sequence,
            request_nonce=request_nonce,
            timestamp=ts,
        )
        sig = compute_poll_signature(k_poll, canonical)
        result = self.pm.check_authenticated_poll_status(
            session_id=session_id,
            sequence=sequence,
            request_nonce=request_nonce,
            timestamp=ts,
            signature=sig,
        )
        return result, request_nonce

    def _assert_signed_payload(
        self,
        data: dict,
        k_poll: bytes,
        *,
        response_context: str,
        sequence: int,
        request_nonce: bytes,
    ) -> bytes:
        canonical = build_canonical_poll_response(
            version=PAIRING_PROTOCOL_VERSION,
            response_context=response_context,
            session_id=data["session_id"],
            http_status=data["http_status"],
            sequence=sequence,
            request_nonce=request_nonce,
            status=data["status"],
            expires_in=data["expires_in"],
            device_id=data["device_id"],
            credential_state=data["credential_state"],
            credential_sha256=bytes.fromhex(data["credential_sha256"]),
        )
        self.assertTrue(
            verify_poll_response_signature(
                k_poll, canonical, bytes.fromhex(data["response_sig"])
            )
        )
        return canonical

    # ── Offer Creation ─────────────────────────────────────────────────────
    def test_create_offer(self):
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        self.assertEqual(self.pm.state, BridgeState.OFFERING)
        self.assertEqual(offer.lan_ip, self.lan_ip)
        self.assertEqual(offer.port, self.port)
        self.assertTrue(offer.session_id.startswith("sess_"))
        self.assertTrue(offer.qr_payload.startswith("kivo-pair://v1?"))
        self.assertTrue(offer.qr_data_url.startswith("data:image/png;base64,"))
        self.assertFalse(offer.is_expired())

    # ── Handshake with Valid Proof ─────────────────────────────────────────
    def test_handshake_valid_proof_enters_confirming(self):
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_device_123"

        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=offer.session_id,
            cert_fp=offer.cert_fingerprint,
            server_nonce=offer.server_nonce,
            client_nonce=client_nonce,
            client_id=client_id,
            algorithm_id=ALGORITHM_ID,
        )
        proof = compute_client_proof(offer.pairing_secret, context)

        ok, err, sas = self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id=client_id,
            device_name="Pixel 8 Pro",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=proof,
        )

        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertIsNotNone(sas)
        self.assertEqual(len(sas.replace(" ", "")), 6)
        self.assertEqual(self.pm.state, BridgeState.CONFIRMING)
        self.assertIsNotNone(self.pm.pending_request)
        self.assertEqual(self.pm.pending_request.sas_code, sas)

    # ── Handshake with Invalid Proof Rejected ──────────────────────────────
    def test_handshake_invalid_proof_rejected(self):
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        bad_proof = b"\x00" * 32

        ok, err, sas = self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id="client_1",
            device_name="Unknown",
            client_ip="192.168.1.99",
            client_nonce=client_nonce,
            client_proof=bad_proof,
        )

        self.assertFalse(ok)
        self.assertEqual(err, "invalid_proof")
        self.assertIsNone(sas)
        self.assertEqual(self.pm.state, BridgeState.OFFERING)

    # ── Desktop Approval ───────────────────────────────────────────────────
    def test_desktop_approval_establishes_session(self):
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_1"

        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=offer.session_id,
            cert_fp=offer.cert_fingerprint,
            server_nonce=offer.server_nonce,
            client_nonce=client_nonce,
            client_id=client_id,
            algorithm_id=ALGORITHM_ID,
        )
        proof = compute_client_proof(offer.pairing_secret, context)

        self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id=client_id,
            device_name="Pixel",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=proof,
        )

        # Desktop user clicks Approve
        ok, session = self.pm.approve_pairing(offer.session_id)
        self.assertTrue(ok)
        self.assertIsNotNone(session)
        self.assertEqual(self.pm.state, BridgeState.PAIRED)
        self.assertTrue(session.bearer_token.startswith("sess_tok_"))
        self.assertFalse(session.is_expired())

        # One-time pairing secret was burned
        self.assertIsNone(self.pm.current_offer)

    # ── Desktop Rejection ──────────────────────────────────────────────────
    def test_desktop_rejection(self):
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=offer.session_id,
            cert_fp=offer.cert_fingerprint,
            server_nonce=offer.server_nonce,
            client_nonce=client_nonce,
            client_id="c1",
            algorithm_id=ALGORITHM_ID,
        )
        proof = compute_client_proof(offer.pairing_secret, context)
        self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id="c1",
            device_name="Rogue Phone",
            client_ip="192.168.1.88",
            client_nonce=client_nonce,
            client_proof=proof,
        )

        # User rejects
        rejected = self.pm.reject_pairing(offer.session_id)
        self.assertTrue(rejected)
        self.assertEqual(self.pm.state, BridgeState.REJECTED)
        self.assertIsNone(self.pm.active_session)

    # ── Revocation ─────────────────────────────────────────────────────────
    def test_device_revocation(self):
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=offer.session_id,
            cert_fp=offer.cert_fingerprint,
            server_nonce=offer.server_nonce,
            client_nonce=client_nonce,
            client_id="c1",
            algorithm_id=ALGORITHM_ID,
        )
        proof = compute_client_proof(offer.pairing_secret, context)
        self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id="c1",
            device_name="Pixel",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=proof,
        )
        self.pm.approve_pairing(offer.session_id)
        self.assertEqual(self.pm.state, BridgeState.PAIRED)

        # Revoke
        revoked = self.pm.revoke_device(offer.session_id)
        self.assertTrue(revoked)
        self.assertEqual(self.pm.state, BridgeState.REVOKED)
        self.assertIsNone(self.pm.active_session)

    # ── Rate Limiting (Max 5 attempts / min) ────────────────────────────────
    def test_handshake_rate_limiting(self):
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        ip = "192.168.1.199"

        # 5 failed attempts with invalid session ID
        for _ in range(5):
            self.pm.handle_client_handshake(
                session_id="wrong_session_id",
                client_id="bad",
                device_name="Attacker",
                client_ip=ip,
                client_nonce=os.urandom(32),
                client_proof=b"\x00" * 32,
            )

        # 6th attempt should be rate limited
        ok, err, _ = self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id="bad",
            device_name="Attacker",
            client_ip=ip,
            client_nonce=os.urandom(32),
            client_proof=b"\x00" * 32,
        )
        self.assertFalse(ok)
        self.assertEqual(err, "rate_limited")

    # ── Token Hardening & Replay Rejection Tests (Stage 13.3.1.1) ──────────

    def test_invalid_proof_does_not_consume_token(self):
        """Invalid proof fails but leaves the offer valid for legitimate phone retry."""
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_1"

        # 1. Invalid proof attempt
        ok1, err1, _ = self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id=client_id,
            device_name="Client",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=b"\x00" * 32,
        )
        self.assertFalse(ok1)
        self.assertEqual(err1, "invalid_proof")
        # Offer is still valid in OFFERING state!
        self.assertEqual(self.pm.state, BridgeState.OFFERING)

        # 2. Subsequent valid proof succeeds
        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=offer.session_id,
            cert_fp=offer.cert_fingerprint,
            server_nonce=offer.server_nonce,
            client_nonce=client_nonce,
            client_id=client_id,
            algorithm_id=ALGORITHM_ID,
        )
        valid_proof = compute_client_proof(offer.pairing_secret, context)

        ok2, err2, sas = self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id=client_id,
            device_name="Client",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=valid_proof,
        )
        self.assertTrue(ok2)
        self.assertIsNone(err2)
        self.assertEqual(self.pm.state, BridgeState.CONFIRMING)

    def test_unsupported_version_does_not_consume_token(self):
        """Unsupported version fails but does not destroy pairing offer."""
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_1"

        ok, err, _ = self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id=client_id,
            device_name="Client",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=b"\x00" * 32,
            version="999",
        )
        self.assertFalse(ok)
        self.assertEqual(err, "unsupported_protocol_version")
        self.assertEqual(self.pm.state, BridgeState.OFFERING)

    def test_invalid_session_id_does_not_consume_token(self):
        """Request with wrong session ID is rejected without invalidating the active offer."""
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        ok, err, _ = self.pm.handle_client_handshake(
            session_id="sess_different_id_here",
            client_id="client_1",
            device_name="Client",
            client_ip="192.168.1.45",
            client_nonce=os.urandom(32),
            client_proof=b"\x00" * 32,
        )
        self.assertFalse(ok)
        self.assertEqual(err, "invalid_session_id")
        self.assertEqual(self.pm.state, BridgeState.OFFERING)

    def test_replayed_valid_proof_rejected(self):
        """A valid proof consumes token; replay attempt is strictly rejected with token_already_consumed."""
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_1"
        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=offer.session_id,
            cert_fp=offer.cert_fingerprint,
            server_nonce=offer.server_nonce,
            client_nonce=client_nonce,
            client_id=client_id,
            algorithm_id=ALGORITHM_ID,
        )
        valid_proof = compute_client_proof(offer.pairing_secret, context)

        # 1. First valid handshake consumes token
        ok1, _, _ = self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id=client_id,
            device_name="Phone",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=valid_proof,
        )
        self.assertTrue(ok1)

        # 2. Replay of identical handshake is rejected
        ok2, err2, _ = self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id=client_id,
            device_name="Phone",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=valid_proof,
        )
        self.assertFalse(ok2)
        self.assertEqual(err2, "token_already_consumed")

    def test_concurrent_handshakes_produce_exactly_one_candidate(self):
        """Two concurrent valid handshakes using same token: exactly one succeeds."""
        import threading
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_id = "client_concurrent"

        results = []

        def worker(idx):
            client_nonce = os.urandom(32)
            context = build_canonical_context(
                version=PAIRING_PROTOCOL_VERSION,
                session_id=offer.session_id,
                cert_fp=offer.cert_fingerprint,
                server_nonce=offer.server_nonce,
                client_nonce=client_nonce,
                client_id=client_id,
                algorithm_id=ALGORITHM_ID,
            )
            proof = compute_client_proof(offer.pairing_secret, context)
            res = self.pm.handle_client_handshake(
                session_id=offer.session_id,
                client_id=client_id,
                device_name=f"Device-{idx}",
                client_ip=f"192.168.1.{50+idx}",
                client_nonce=client_nonce,
                client_proof=proof,
            )
            results.append(res)

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r[0] is True]
        failures = [r for r in results if r[0] is False]

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][1], "token_already_consumed")

    def test_desktop_rejection_permanently_consumes_token(self):
        """Desktop rejection burns the token; cannot be reused."""
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "c1"
        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=offer.session_id,
            cert_fp=offer.cert_fingerprint,
            server_nonce=offer.server_nonce,
            client_nonce=client_nonce,
            client_id=client_id,
            algorithm_id=ALGORITHM_ID,
        )
        proof = compute_client_proof(offer.pairing_secret, context)
        self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id=client_id,
            device_name="Client",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=proof,
        )
        self.pm.reject_pairing(offer.session_id)

        # Re-attempt with same session_id is rejected
        ok, err, _ = self.pm.handle_client_handshake(
            session_id=offer.session_id,
            client_id=client_id,
            device_name="Client",
            client_ip="192.168.1.45",
            client_nonce=client_nonce,
            client_proof=proof,
        )
        self.assertFalse(ok)
        self.assertEqual(err, "token_already_consumed")

    def test_pairing_reset_invalidates_token(self):
        """Resetting pairing state invalidates active offer."""
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        sess_id = offer.session_id
        self.pm.reset_locked()

        ok, err, _ = self.pm.handle_client_handshake(
            session_id=sess_id,
            client_id="c1",
            device_name="Client",
            client_ip="192.168.1.45",
            client_nonce=os.urandom(32),
            client_proof=b"\x00" * 32,
        )
        self.assertFalse(ok)
        self.assertIn(err, ("no_active_offer", "token_already_consumed"))

    def test_failed_proofs_per_ip_lockout_preserves_offer_for_legitimate_client(self):
        """Attacker IP sending 5 bad proofs is locked out, but legitimate IP can still pair."""
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        sess_id = offer.session_id
        attacker_ip = "192.168.1.100"

        # Clear rate limit window for testing proof failures specifically
        for idx in range(4):
            ok, err, _ = self.pm.handle_client_handshake(
                session_id=sess_id,
                client_id="attacker",
                device_name="Attacker",
                client_ip=attacker_ip,
                client_nonce=os.urandom(32),
                client_proof=b"\x00" * 32,
            )
            self.assertFalse(ok)
            self.assertEqual(err, "invalid_proof")

        # 5th bad proof locks out attacker IP
        ok5, err5, _ = self.pm.handle_client_handshake(
            session_id=sess_id,
            client_id="attacker",
            device_name="Attacker",
            client_ip=attacker_ip,
            client_nonce=os.urandom(32),
            client_proof=b"\x00" * 32,
        )
        self.assertFalse(ok5)
        self.assertEqual(err5, "ip_locked_out")

        # 6th attempt by attacker is rejected due to IP lockout
        ok6, err6, _ = self.pm.handle_client_handshake(
            session_id=sess_id,
            client_id="attacker",
            device_name="Attacker",
            client_ip=attacker_ip,
            client_nonce=os.urandom(32),
            client_proof=b"\x00" * 32,
        )
        self.assertFalse(ok6)
        self.assertEqual(err6, "ip_locked_out")

        # Legitimate client on different IP can still pair successfully!
        legit_ip = "192.168.1.50"
        legit_nonce = os.urandom(32)
        legit_id = "legit_phone"
        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=sess_id,
            cert_fp=offer.cert_fingerprint,
            server_nonce=offer.server_nonce,
            client_nonce=legit_nonce,
            client_id=legit_id,
            algorithm_id=ALGORITHM_ID,
        )
        proof = compute_client_proof(offer.pairing_secret, context)

        ok_legit, err_legit, sas = self.pm.handle_client_handshake(
            session_id=sess_id,
            client_id=legit_id,
            device_name="Legit Phone",
            client_ip=legit_ip,
            client_nonce=legit_nonce,
            client_proof=proof,
        )
        self.assertTrue(ok_legit)
        self.assertIsNone(err_legit)
        self.assertEqual(self.pm.state, BridgeState.CONFIRMING)

    def test_total_failed_proofs_across_ips_locks_offer(self):
        """25 distributed bad proofs invalidate the offer to stop brute force."""
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        sess_id = offer.session_id

        for idx in range(24):
            ok, err, _ = self.pm.handle_client_handshake(
                session_id=sess_id,
                client_id="c1",
                device_name="Client",
                client_ip=f"192.168.{idx // 200 + 1}.{idx % 200 + 10}",
                client_nonce=os.urandom(32),
                client_proof=b"\x00" * 32,
            )
            self.assertFalse(ok)
            self.assertEqual(err, "invalid_proof")

        # 25th bad proof across distributed IPs invalidates offer
        ok25, err25, _ = self.pm.handle_client_handshake(
            session_id=sess_id,
            client_id="c1",
            device_name="Client",
            client_ip="192.168.10.99",
            client_nonce=os.urandom(32),
            client_proof=b"\x00" * 32,
        )
        self.assertFalse(ok25)
        self.assertEqual(err25, "too_many_failed_attempts")
        self.assertEqual(self.pm.state, BridgeState.EXPIRED)

    def test_rate_limit_state_remains_bounded(self):
        """Rate limit memory does not leak or grow unbounded."""
        offer = self.pm.create_offer(self.lan_ip, self.port, self.cert_fp)
        # Record attempts from 150 different IPs
        now = time.time()
        for i in range(150):
            self.pm._record_failed_attempt(f"192.168.2.{i}", now)
        # Memory must remain capped
        self.assertLessEqual(len(self.pm._failed_attempts), 100)

    # ── Approval Response V2 Integrity ─────────────────────────────────────
    def test_pending_poll_response_is_signed_without_credential(self):
        offer, _, _, k_poll = self._handshake()
        (ok, status, data), nonce = self._poll(offer.session_id, k_poll, 1)

        self.assertTrue(ok)
        self.assertEqual(status, "pending_approval")
        self.assertEqual(data["response_context"], "poll")
        self.assertEqual(data["http_status"], 200)
        self.assertEqual(data["credential_state"], "none")
        self.assertEqual(data["credential_sha256"], "")
        self.assertNotIn("session_token", data)
        self._assert_signed_payload(
            data, k_poll, response_context="poll", sequence=1, request_nonce=nonce
        )

    def test_approved_poll_response_signs_credential_hash_and_metadata(self):
        offer, client_nonce, _, k_poll = self._handshake(approve=True)
        (ok, status, data), nonce = self._poll(offer.session_id, k_poll, 1)

        self.assertTrue(ok)
        self.assertEqual(status, "approved")
        self.assertEqual(data["credential_state"], "delivered")
        self.assertEqual(data["device_id"], "client_device_123")
        self.assertGreater(data["expires_in"], 0)
        self.assertEqual(
            data["credential_sha256"],
            compute_credential_sha256(data["session_token"]).hex(),
        )
        self._assert_signed_payload(
            data, k_poll, response_context="poll", sequence=1, request_nonce=nonce
        )

        # Handshake response uses sequence 0 + client nonce and is also signed.
        hs = self.pm.build_handshake_response(offer.session_id, client_nonce)
        self.assertIsNotNone(hs)
        self.assertEqual(hs["credential_state"], "delivered")
        self._assert_signed_payload(
            hs,
            k_poll,
            response_context="handshake",
            sequence=0,
            request_nonce=client_nonce,
        )

    def test_invalid_signature_does_not_consume_sequence_or_nonce(self):
        offer, _, _, k_poll = self._handshake()
        nonce = os.urandom(16)
        ts = time.time()
        canonical = build_canonical_poll_request(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=offer.session_id,
            sequence=1,
            request_nonce=nonce,
            timestamp=ts,
        )
        bad_sig = bytearray(compute_poll_signature(k_poll, canonical))
        bad_sig[0] ^= 0xFF

        ok, err, _ = self.pm.check_authenticated_poll_status(
            session_id=offer.session_id,
            sequence=1,
            request_nonce=nonce,
            timestamp=ts,
            signature=bytes(bad_sig),
        )
        self.assertFalse(ok)
        self.assertEqual(err, "invalid_signature")

        # A later valid request with the same nonce and sequence still succeeds.
        result, _ = self._poll(
            offer.session_id, k_poll, 1, nonce=nonce, timestamp=ts
        )
        self.assertTrue(result[0])
        self.assertEqual(result[1], "pending_approval")

    def test_recovery_window_expiry_purges_polling_material(self):
        offer, _, _, k_poll = self._handshake(approve=True)
        with self.pm._lock:
            self.pm._approval_time[offer.session_id] = (
                time.time() - 31.0
            )

        ok, err, data = self._poll(offer.session_id, k_poll, 1)[0]
        self.assertFalse(ok)
        self.assertEqual(err, "polling_window_closed")
        self.assertIsNone(data)
        self.assertNotIn(offer.session_id, self.pm._poll_keys)
        self.assertNotIn(offer.session_id, self.pm._poll_sequences)
        self.assertNotIn(offer.session_id, self.pm._poll_nonces)

    def test_revoked_session_cannot_retrieve_credentials(self):
        offer, _, _, k_poll = self._handshake(approve=True)
        (ok, status, data), _ = self._poll(offer.session_id, k_poll, 1)
        self.assertTrue(ok)
        self.assertEqual(status, "approved")

        self.assertTrue(self.pm.revoke_device(offer.session_id))
        ok2, err2, data2 = self.pm.check_authenticated_poll_status(
            session_id=offer.session_id,
            sequence=2,
            request_nonce=os.urandom(16),
            timestamp=time.time(),
            signature=b"\x00" * 32,
        )
        self.assertFalse(ok2)
        self.assertIn(err2, ("unknown_or_expired_polling", "unknown_session"))
        self.assertIsNone(data2)
        self.assertNotIn(offer.session_id, self.pm._poll_keys)

    def test_rejected_pairing_purges_polling_state(self):
        offer, _, _, k_poll = self._handshake()
        self.assertTrue(self.pm.reject_pairing(offer.session_id))
        self.assertNotIn(offer.session_id, self.pm._poll_keys)
        self.assertNotIn(offer.session_id, self.pm._cached_session_keys)

        ok, err, data = self.pm.check_authenticated_poll_status(
            session_id=offer.session_id,
            sequence=1,
            request_nonce=os.urandom(16),
            timestamp=time.time(),
            signature=b"\x00" * 32,
        )
        self.assertFalse(ok)
        self.assertIn(err, ("unknown_or_expired_polling", "rejected"))
        self.assertIsNone(data)

    def test_reset_clears_all_polling_material(self):
        offer, _, _, _ = self._handshake(approve=True)
        self.assertIn(offer.session_id, self.pm._poll_keys)
        self.pm.reset_locked()
        self.assertEqual(self.pm._poll_keys, {})
        self.assertEqual(self.pm._poll_sequences, {})
        self.assertEqual(self.pm._poll_nonces, {})
        self.assertEqual(self.pm._cached_session_keys, {})
        self.assertEqual(self.pm._approval_time, {})


if __name__ == "__main__":
    unittest.main()
