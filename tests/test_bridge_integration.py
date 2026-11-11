"""End-to-end loopback integration tests for Phone Bridge pairing protocol."""
from __future__ import annotations

import asyncio
import base64
import os
import ssl
import threading
import time
import unittest
from unittest.mock import MagicMock

import httpx

from kivo.bridge.crypto import (
    build_canonical_context,
    build_canonical_poll_request,
    build_canonical_poll_response,
    compute_client_proof,
    compute_poll_response_signature,
    compute_poll_signature,
    derive_poll_key,
    derive_sas_code,
    generate_ephemeral_certificate,
    verify_poll_response_signature,
)
from kivo.bridge.models import (
    ALGORITHM_ID,
    PAIRING_PROTOCOL_VERSION,
    BridgeState,
)
from kivo.bridge.pairing import PairingManager
from kivo.bridge.server import BridgeServer


def build_status_request(
    session_id: str,
    sequence: int,
    k_poll: bytes,
    timestamp: float | None = None,
) -> tuple[dict[str, object], bytes, bytes]:
    """Build a fresh signed status-poll request body and its canonical transcript."""
    request_nonce = os.urandom(16)
    ts = time.time() if timestamp is None else timestamp
    canonical = build_canonical_poll_request(
        version=PAIRING_PROTOCOL_VERSION,
        session_id=session_id,
        sequence=sequence,
        request_nonce=request_nonce,
        timestamp=ts,
    )
    sig = compute_poll_signature(k_poll, canonical)
    payload = {
        "session_id": session_id,
        "sequence": sequence,
        "request_nonce_b64": base64.urlsafe_b64encode(request_nonce)
        .decode("ascii")
        .rstrip("="),
        "timestamp": ts,
        "signature_hex": sig.hex(),
    }
    return payload, canonical, request_nonce


def assert_signed_response(
    testcase: unittest.TestCase,
    data: dict,
    k_poll: bytes,
    response_context: str,
    sequence: int,
    request_nonce: bytes,
) -> bytes:
    """Verify the V2 response MAC and credential hash against the client context."""
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
    sig = bytes.fromhex(data["response_sig"])
    testcase.assertTrue(verify_poll_response_signature(k_poll, canonical, sig))

    if data["credential_state"] == "delivered":
        from kivo.bridge.crypto import compute_credential_sha256

        testcase.assertEqual(
            data["credential_sha256"],
            compute_credential_sha256(data["session_token"]).hex(),
        )
    else:
        testcase.assertEqual(data["credential_state"], "none")
        testcase.assertNotIn("session_token", data)

    return canonical


class BridgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pairing_manager = PairingManager()
        self.lan_ip = "127.0.0.1"  # Loopback only for testing
        self.port = 18443

        self.cert, self.key, self.cert_fp, self.cert_pem, self.key_pem = (
            generate_ephemeral_certificate(self.lan_ip)
        )

        self.pairing_requested_event = threading.Event()
        self.last_pairing_request: dict | None = None

        def on_req(req):
            self.last_pairing_request = req
            self.pairing_requested_event.set()

        self.server = BridgeServer(
            pairing_manager=self.pairing_manager,
            lan_ip=self.lan_ip,
            port=self.port,
            cert_pem=self.cert_pem,
            key_pem=self.key_pem,
            on_pairing_requested=on_req,
        )
        self.server.start()
        await asyncio.sleep(0.3)

        # SSL context trusting our ephemeral cert for the test client
        self.client_ssl = ssl.create_default_context()
        self.client_ssl.load_verify_locations(cadata=self.cert_pem.decode("ascii"))

    async def asyncTearDown(self) -> None:
        self.server.stop(timeout=2.0)
        await asyncio.sleep(0.1)

    async def test_end_to_end_handshake_and_approval_flow(self):
        offer = self.pairing_manager.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_android_test"
        device_name = "Pixel 8 Pro"

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
        _, expected_sas = derive_sas_code(offer.pairing_secret, offer.server_nonce, context)

        payload = {
            "version": PAIRING_PROTOCOL_VERSION,
            "session_id": offer.session_id,
            "client_id": client_id,
            "device_name": device_name,
            "client_nonce_b64": base64.urlsafe_b64encode(client_nonce).decode("ascii").rstrip("="),
            "client_proof_b64": base64.urlsafe_b64encode(proof).decode("ascii").rstrip("="),
        }

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            # Launch handshake in background task (as it long-polls for approval)
            handshake_task = asyncio.create_task(
                client.post(
                    f"https://{self.lan_ip}:{self.port}/api/v1/pair/handshake",
                    json=payload,
                    timeout=10.0,
                )
            )

            # Wait for desktop callback
            await asyncio.to_thread(self.pairing_requested_event.wait, 5.0)
            self.assertIsNotNone(self.last_pairing_request)
            self.assertEqual(self.last_pairing_request["sas_code"], expected_sas)

            # Desktop user clicks Approve
            self.pairing_manager.approve_pairing(offer.session_id)

            # Await handshake completion
            response = await handshake_task
            self.assertEqual(response.status_code, 200)
            resp_json = response.json()
            self.assertTrue(resp_json.get("ok"))
            token = resp_json.get("session_token")
            self.assertTrue(token.startswith("sess_tok_"))

            # Test authenticated ping with bearer token
            ping_resp = await client.get(
                f"https://{self.lan_ip}:{self.port}/api/v1/ping",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(ping_resp.status_code, 200)
            self.assertTrue(ping_resp.json().get("pong"))

    async def test_end_to_end_handshake_rejection_flow(self):
        offer = self.pairing_manager.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_android_reject"

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

        payload = {
            "version": PAIRING_PROTOCOL_VERSION,
            "session_id": offer.session_id,
            "client_id": client_id,
            "device_name": "Rogue Device",
            "client_nonce_b64": base64.urlsafe_b64encode(client_nonce).decode("ascii").rstrip("="),
            "client_proof_b64": base64.urlsafe_b64encode(proof).decode("ascii").rstrip("="),
        }

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            handshake_task = asyncio.create_task(
                client.post(
                    f"https://{self.lan_ip}:{self.port}/api/v1/pair/handshake",
                    json=payload,
                    timeout=10.0,
                )
            )

            await asyncio.to_thread(self.pairing_requested_event.wait, 5.0)

            # Desktop user clicks Reject
            self.pairing_manager.reject_pairing(offer.session_id)

            response = await handshake_task
            self.assertEqual(response.status_code, 403)

            # Replay attempt fails with 409 Conflict
            replay_resp = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/pair/handshake",
                json=payload,
            )
            self.assertEqual(replay_resp.status_code, 409)

    async def test_poll_status_endpoint(self):
        offer = self.pairing_manager.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_poll_test"

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
        k_poll = derive_poll_key(offer.pairing_secret, client_nonce, context)

        payload = {
            "version": PAIRING_PROTOCOL_VERSION,
            "session_id": offer.session_id,
            "client_id": client_id,
            "device_name": "Poll Device",
            "client_nonce_b64": base64.urlsafe_b64encode(client_nonce).decode("ascii").rstrip("="),
            "client_proof_b64": base64.urlsafe_b64encode(proof).decode("ascii").rstrip("="),
        }

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            # Submit handshake in background
            handshake_task = asyncio.create_task(
                client.post(
                    f"https://{self.lan_ip}:{self.port}/api/v1/pair/handshake",
                    json=payload,
                    timeout=10.0,
                )
            )

            await asyncio.to_thread(self.pairing_requested_event.wait, 5.0)

            # While pending, a fresh authenticated poll succeeds and is signed.
            req1, _, nonce1 = build_status_request(offer.session_id, 1, k_poll)
            status_resp1 = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/pair/status",
                json=req1,
            )
            self.assertEqual(status_resp1.status_code, 200)
            pending_data = status_resp1.json()
            self.assertEqual(pending_data.get("status"), "pending_approval")
            self.assertEqual(pending_data.get("credential_state"), "none")
            self.assertNotIn("session_token", pending_data)
            assert_signed_response(
                self, pending_data, k_poll, "poll", 1, nonce1
            )

            # A legacy static poll_auth request is no longer accepted at all.
            legacy_resp = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/pair/status",
                json={"session_id": offer.session_id, "poll_auth_b64": "A" * 43},
            )
            self.assertEqual(legacy_resp.status_code, 422)

            # Out-of-range sequence/timestamp values are rejected by validation
            # instead of reaching canonical encoding and raising a 500.
            for bad_fields in (
                {"sequence": 2**63},
                {"timestamp": -1.0},
                {"timestamp": 4102444801.0},
            ):
                bad_resp = await client.post(
                    f"https://{self.lan_ip}:{self.port}/api/v1/pair/status",
                    json={
                        "session_id": offer.session_id,
                        "sequence": 1,
                        "request_nonce_b64": "EBESExQVFhcYGRobHB0eHw",
                        "timestamp": time.time(),
                        "signature_hex": "00" * 32,
                        **bad_fields,
                    },
                )
                self.assertEqual(bad_resp.status_code, 422)

            # Desktop approves
            self.pairing_manager.approve_pairing(offer.session_id)
            handshake_resp = await handshake_task
            self.assertEqual(handshake_resp.status_code, 200)
            hs_data = handshake_resp.json()
            assert_signed_response(
                self, hs_data, k_poll, "handshake", 0, client_nonce
            )

            # Poll status after approval returns an authenticated credential.
            req2, _, nonce2 = build_status_request(offer.session_id, 2, k_poll)
            status_resp2 = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/pair/status",
                json=req2,
            )
            self.assertEqual(status_resp2.status_code, 200)
            approved_data = status_resp2.json()
            self.assertEqual(approved_data.get("status"), "approved")
            self.assertEqual(approved_data.get("credential_state"), "delivered")
            self.assertTrue(approved_data.get("session_token").startswith("sess_tok_"))
            self.assertEqual(
                approved_data.get("session_token"), hs_data.get("session_token")
            )
            assert_signed_response(
                self, approved_data, k_poll, "poll", 2, nonce2
            )

    async def test_fresh_authenticated_poll_status_flow(self):
        offer = self.pairing_manager.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_fresh_poll"

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
        k_poll = derive_poll_key(offer.pairing_secret, client_nonce, context)

        payload = {
            "version": PAIRING_PROTOCOL_VERSION,
            "session_id": offer.session_id,
            "client_id": client_id,
            "device_name": "Poll Device",
            "client_nonce_b64": base64.urlsafe_b64encode(client_nonce).decode("ascii").rstrip("="),
            "client_proof_b64": base64.urlsafe_b64encode(proof).decode("ascii").rstrip("="),
        }

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            handshake_task = asyncio.create_task(
                client.post(
                    f"https://{self.lan_ip}:{self.port}/api/v1/pair/handshake",
                    json=payload,
                    timeout=10.0,
                )
            )

            await asyncio.to_thread(self.pairing_requested_event.wait, 5.0)

            # 1. Fresh poll sequence 1
            now_ts = round(time.time(), 3)
            nonce1 = os.urandom(16)
            canonical1 = build_canonical_poll_request(
                version=PAIRING_PROTOCOL_VERSION,
                session_id=offer.session_id,
                sequence=1,
                request_nonce=nonce1,
                timestamp=now_ts,
            )
            sig1 = compute_poll_signature(k_poll, canonical1)

            poll_payload1 = {
                "session_id": offer.session_id,
                "sequence": 1,
                "request_nonce_b64": base64.urlsafe_b64encode(nonce1).decode("ascii").rstrip("="),
                "timestamp": now_ts,
                "signature_hex": sig1.hex(),
            }
            res1 = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/pair/status",
                json=poll_payload1,
            )
            self.assertEqual(res1.status_code, 200)
            self.assertEqual(res1.json().get("status"), "pending_approval")

            # 2. Replay of sequence 1 is rejected with 409 Conflict
            res_replay_seq = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/pair/status",
                json=poll_payload1,
            )
            self.assertEqual(res_replay_seq.status_code, 409)

            # 3. Sequence 2 with replayed nonce1 is rejected with 409 Conflict
            canonical_replay_nonce = build_canonical_poll_request(
                version=PAIRING_PROTOCOL_VERSION,
                session_id=offer.session_id,
                sequence=2,
                request_nonce=nonce1,
                timestamp=now_ts,
            )
            sig_replay_nonce = compute_poll_signature(k_poll, canonical_replay_nonce)
            res_replay_nonce = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/pair/status",
                json={
                    "session_id": offer.session_id,
                    "sequence": 2,
                    "request_nonce_b64": base64.urlsafe_b64encode(nonce1).decode("ascii").rstrip("="),
                    "timestamp": now_ts,
                    "signature_hex": sig_replay_nonce.hex(),
                },
            )
            self.assertEqual(res_replay_nonce.status_code, 409)

            # 4. Valid sequence 2 with fresh nonce succeeds
            nonce2 = os.urandom(16)
            canonical2 = build_canonical_poll_request(
                version=PAIRING_PROTOCOL_VERSION,
                session_id=offer.session_id,
                sequence=2,
                request_nonce=nonce2,
                timestamp=now_ts,
            )
            sig2 = compute_poll_signature(k_poll, canonical2)
            res2 = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/pair/status",
                json={
                    "session_id": offer.session_id,
                    "sequence": 2,
                    "request_nonce_b64": base64.urlsafe_b64encode(nonce2).decode("ascii").rstrip("="),
                    "timestamp": now_ts,
                    "signature_hex": sig2.hex(),
                },
            )
            self.assertEqual(res2.status_code, 200)

            # 5. Desktop user approves pairing
            self.pairing_manager.approve_pairing(offer.session_id)
            await handshake_task

            # 6. Poll with sequence 3 receives authenticated approved response
            nonce3 = os.urandom(16)
            canonical3 = build_canonical_poll_request(
                version=PAIRING_PROTOCOL_VERSION,
                session_id=offer.session_id,
                sequence=3,
                request_nonce=nonce3,
                timestamp=now_ts,
            )
            sig3 = compute_poll_signature(k_poll, canonical3)
            res3 = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/pair/status",
                json={
                    "session_id": offer.session_id,
                    "sequence": 3,
                    "request_nonce_b64": base64.urlsafe_b64encode(nonce3).decode("ascii").rstrip("="),
                    "timestamp": now_ts,
                    "signature_hex": sig3.hex(),
                },
            )
            self.assertEqual(res3.status_code, 200)
            resp_data = res3.json()
            self.assertEqual(resp_data.get("status"), "approved")
            self.assertEqual(resp_data.get("credential_state"), "delivered")

            # Client verifies the full V2 response transcript before storing credentials.
            assert_signed_response(self, resp_data, k_poll, "poll", 3, nonce3)

            # Changing the credential without updating the signed hash must fail.
            from kivo.bridge.crypto import compute_credential_sha256
            self.assertNotEqual(
                compute_credential_sha256("sess_tok_attacker").hex(),
                resp_data["credential_sha256"],
            )

            # Changing any signed field must invalidate the MAC.
            tampered = dict(resp_data)
            tampered["expires_in"] = int(resp_data["expires_in"]) + 1
            with self.assertRaises(AssertionError):
                assert_signed_response(self, tampered, k_poll, "poll", 3, nonce3)


if __name__ == "__main__":
    unittest.main()
