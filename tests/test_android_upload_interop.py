"""Byte-level and live-transcript interoperability for the Android upload client.

The fixture is shared with the Kotlin unit tests, so every canonical request and
response byte plus every HMAC is checked from both languages. The live test uses
the production desktop service object and its local approval interface; it does
not bypass Gate A or Gate B.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from kivo.bridge.crypto import (
    build_canonical_context,
    compute_client_proof,
)
from kivo.bridge.models import ALGORITHM_ID, PAIRING_PROTOCOL_VERSION
from kivo.bridge.quarantine import QuarantineManager
from kivo.bridge.service import PhoneBridgeService
from kivo.bridge.transfer_crypto import (
    TRANSFER_PROTOCOL_VERSION,
    build_canonical_transfer_request,
    build_canonical_transfer_response,
    compute_transfer_request_signature,
    compute_transfer_response_signature,
    encode_nonce_b64,
    verify_transfer_request_signature,
    verify_transfer_response_signature,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "transfer_test_vectors.json").read_text()
)
K_UP = bytes.fromhex(FIXTURES["upstream_key_hex"])
K_DOWN = bytes.fromhex(FIXTURES["downstream_key_hex"])
SESSION_ID = FIXTURES["session_id"]


class TransferFixtureInteropTests(unittest.TestCase):
    """Recompute every Android-facing vector with the Python implementation."""

    def test_exact_android_request_vectors(self):
        cases = {
            "exact_upload_init": FIXTURES["exact_upload_init"],
            "exact_upload_chunk": FIXTURES["exact_upload_chunk"],
            "upload_init": FIXTURES["upload_init"],
            "upload_chunk": FIXTURES["upload_chunk"],
            "upload_status": FIXTURES["upload_status"],
            "upload_cancel": FIXTURES["upload_cancel"],
        }
        for name, vector in cases.items():
            if "body_utf8" in vector:
                body = vector["body_utf8"].encode("utf-8")
                self.assertEqual(vector["content_length"], len(body))
                self.assertEqual(
                    vector["content_sha256_hex"], hashlib.sha256(body).hexdigest()
                )
            elif "body_b64" in vector:
                body = base64.b64decode(vector["body_b64"])
                self.assertEqual(vector["content_length"], len(body))
                self.assertEqual(
                    vector["content_sha256_hex"], hashlib.sha256(body).hexdigest()
                )
            else:
                body = b""

            canonical = build_canonical_transfer_request(
                version=TRANSFER_PROTOCOL_VERSION,
                session_id=SESSION_ID,
                method=vector["method"],
                endpoint=vector["endpoint"],
                sequence=vector["sequence"],
                nonce=bytes.fromhex(vector["nonce_hex"]),
                timestamp=vector["timestamp"],
                content_length=vector["content_length"],
                content_sha256=bytes.fromhex(vector["content_sha256_hex"]),
            )
            self.assertEqual(vector["canonical_request_hex"], canonical.hex(), name)
            signature = compute_transfer_request_signature(K_UP, canonical)
            self.assertEqual(vector["signature_hex"], signature.hex(), name)
            self.assertTrue(verify_transfer_request_signature(K_UP, canonical, signature))
            self.assertFalse(verify_transfer_request_signature(K_DOWN, canonical, signature))

    def test_exact_android_response_vector(self):
        vector = FIXTURES["exact_response"]
        payload = base64.b64decode(vector["payload_b64"])
        self.assertEqual(vector["payload_utf8"], payload.decode("utf-8"))
        self.assertEqual(vector["payload_length"], len(payload))
        self.assertEqual(
            vector["payload_sha256_hex"], hashlib.sha256(payload).hexdigest()
        )

        canonical = build_canonical_transfer_response(
            version=TRANSFER_PROTOCOL_VERSION,
            response_context=vector["response_context"],
            session_id=SESSION_ID,
            http_status=vector["http_status"],
            sequence=vector["sequence"],
            nonce=bytes.fromhex(vector["nonce_hex"]),
            state=vector["state"],
            transfer_id=vector["transfer_id"],
            payload_length=len(payload),
            payload_sha256=hashlib.sha256(payload).digest(),
        )
        self.assertEqual(vector["canonical_response_hex"], canonical.hex())
        signature = compute_transfer_response_signature(K_DOWN, canonical)
        self.assertEqual(vector["signature_hex"], signature.hex())
        self.assertTrue(verify_transfer_response_signature(K_DOWN, canonical, signature))
        self.assertFalse(verify_transfer_response_signature(K_UP, canonical, signature))

    def test_fixture_nonce_encoding_is_unpadded_base64url(self):
        for name in ("exact_upload_init", "exact_upload_chunk", "upload_init"):
            vector = FIXTURES[name]
            nonce = bytes.fromhex(vector["nonce_hex"])
            self.assertEqual(vector["nonce_b64"], encode_nonce_b64(nonce))
            self.assertNotIn("=", vector["nonce_b64"])


class AndroidUploadLiveTranscriptTests(unittest.IsolatedAsyncioTestCase):
    """Drive the production desktop service exactly as the Android client does."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        downloads = root / "Downloads"
        downloads.mkdir(parents=True)
        quarantine = QuarantineManager(
            runtime_base=str(root / "runtime"),
            cache_base=str(root / "cache"),
            downloads_dir=str(downloads),
            free_space_fn=lambda _path: 20 * 1024**3,
        )
        self.quarantine = quarantine
        self.service = PhoneBridgeService(quarantine=quarantine)
        with patch(
            "kivo.bridge.service.discover_lan_interface",
            return_value=("lo", "127.0.0.1"),
        ):
            result = self.service.start_bridge()
        self.assertTrue(result.get("ok"), result)
        self.assertIsNotNone(self.service._server)
        await asyncio.sleep(0.3)

        status = self.service.get_status()
        self.assertIsNotNone(status.lan_ip)
        self.cert_pem = self.service._server._cert_pem
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.load_verify_locations(cadata=self.cert_pem.decode("ascii"))
        self.base_url = f"https://127.0.0.1:{status.port}"
        self.offer = self.service._pairing_manager.current_offer
        self.assertIsNotNone(self.offer)

        self.session = await self._pair_device()
        self.sequences = 0

    async def asyncTearDown(self) -> None:
        try:
            self.service.close(timeout=2.0)
        finally:
            self.tmp.cleanup()
        await asyncio.sleep(0.1)

    async def _pair_device(self):
        client_nonce = os.urandom(32)
        client_id = "client_android_transcript"
        device_name = "Android Emulator"
        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=self.offer.session_id,
            cert_fp=self.offer.cert_fingerprint,
            server_nonce=self.offer.server_nonce,
            client_nonce=client_nonce,
            client_id=client_id,
            algorithm_id=ALGORITHM_ID,
        )
        proof = compute_client_proof(self.offer.pairing_secret, context)
        payload = {
            "version": PAIRING_PROTOCOL_VERSION,
            "session_id": self.offer.session_id,
            "client_id": client_id,
            "device_name": device_name,
            "client_nonce_b64": base64.urlsafe_b64encode(client_nonce)
            .decode("ascii")
            .rstrip("="),
            "client_proof_b64": base64.urlsafe_b64encode(proof)
            .decode("ascii")
            .rstrip("="),
        }

        async with httpx.AsyncClient(verify=self.ssl_context) as client:
            task = asyncio.create_task(
                client.post(
                    f"{self.base_url}/api/v1/pair/handshake",
                    json=payload,
                    timeout=10.0,
                )
            )
            for _ in range(50):
                if self.service._pairing_manager.pending_request is not None:
                    break
                await asyncio.sleep(0.05)
            # This is the production desktop approval interface used by IPC.
            self.assertTrue(self.service.approve_pairing(self.offer.session_id))
            response = await task
        self.assertEqual(response.status_code, 200)
        session = self.service._pairing_manager.active_session
        self.assertIsNotNone(session)
        return session

    def _next_sequence(self) -> int:
        self.sequences += 1
        return self.sequences

    def _headers(
        self,
        method: str,
        endpoint: str,
        content: bytes,
        sequence: int | None = None,
    ) -> tuple[dict[str, str], bytes, int]:
        sequence = self._next_sequence() if sequence is None else sequence
        nonce = os.urandom(16)
        timestamp = time.time()
        content_sha256 = hashlib.sha256(content).digest()
        canonical = build_canonical_transfer_request(
            version=TRANSFER_PROTOCOL_VERSION,
            session_id=self.session.session_id,
            method=method,
            endpoint=endpoint,
            sequence=sequence,
            nonce=nonce,
            timestamp=timestamp,
            content_length=len(content),
            content_sha256=content_sha256,
        )
        signature = compute_transfer_request_signature(
            self.session.upstream_key, canonical
        )
        headers = {
            "Authorization": f"Bearer {self.session.bearer_token}",
            "X-Kivo-Protocol": TRANSFER_PROTOCOL_VERSION,
            "X-Kivo-Session": self.session.session_id,
            "X-Kivo-Sequence": str(sequence),
            "X-Kivo-Nonce": encode_nonce_b64(nonce),
            "X-Kivo-Timestamp": str(timestamp),
            "X-Kivo-Content-SHA256": content_sha256.hex(),
            "X-Kivo-Signature": signature.hex(),
            "Content-Length": str(len(content)),
        }
        return headers, nonce, sequence

    def _verify(self, response, context: str, expected_status: int) -> dict:
        self.assertEqual(response.status_code, expected_status, response.text)
        envelope = response.json()
        self.assertEqual(TRANSFER_PROTOCOL_VERSION, envelope.get("v"))
        self.assertEqual(context, envelope.get("ctx"))
        self.assertEqual(self.session.session_id, envelope.get("sid"))
        self.assertEqual(expected_status, envelope.get("http_status"))
        payload_bytes = base64.b64decode(envelope["payload_b64"])
        self.assertEqual(
            hashlib.sha256(payload_bytes).hexdigest(),
            envelope.get("payload_sha256"),
        )
        canonical = build_canonical_transfer_response(
            version=TRANSFER_PROTOCOL_VERSION,
            response_context=context,
            session_id=self.session.session_id,
            http_status=expected_status,
            sequence=envelope["seq"],
            nonce=base64.urlsafe_b64decode(envelope["nonce_b64"] + "=="),
            state=envelope["state"],
            transfer_id=envelope["tid"],
            payload_length=len(payload_bytes),
            payload_sha256=bytes.fromhex(envelope["payload_sha256"]),
        )
        self.assertTrue(
            verify_transfer_response_signature(
                self.session.downstream_key,
                canonical,
                bytes.fromhex(envelope["sig"]),
            )
        )
        return json.loads(payload_bytes.decode("utf-8"))

    async def test_android_transcript_respects_both_gates_and_resumes_after_lost_ack(self):
        file_content = bytes((index * 7) % 251 for index in range(1024 * 1024 + 12_345))
        file_size = len(file_content)
        file_sha256 = hashlib.sha256(file_content).hexdigest()
        filename = "android_report.pdf"

        init_body = json.dumps(
            {
                "filename": filename,
                "mime": "application/pdf",
                "sha256": file_sha256,
                "size": file_size,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        async with httpx.AsyncClient(verify=self.ssl_context) as client:
            init_headers, _, _ = self._headers(
                "POST", "/api/v1/transfers/uploads/init", init_body
            )
            init_response = await client.post(
                f"{self.base_url}/api/v1/transfers/uploads/init",
                content=init_body,
                headers=init_headers,
            )
            init_payload = self._verify(init_response, "upload_init", 202)
            transfer_id = init_payload["transfer_id"]
            self.assertEqual("awaiting_desktop_approval", init_payload["state"])
            self.assertEqual(filename, init_payload["filename"])
            self.assertEqual(file_size, init_payload["size"])
            self.assertEqual(file_sha256, init_payload["sha256"])

            # No chunk is accepted before Gate A.
            pre_body = file_content[: 1024 * 1024]
            pre_headers, _, _ = self._headers(
                "PUT",
                f"/api/v1/transfers/uploads/{transfer_id}/chunks/0",
                pre_body,
            )
            pre_response = await client.put(
                f"{self.base_url}/api/v1/transfers/uploads/{transfer_id}/chunks/0",
                content=pre_body,
                headers=pre_headers,
            )
            pre_payload = self._verify(pre_response, "upload_chunk", 409)
            self.assertEqual("invalid_transfer_state", pre_payload["error_code"])

            # Gate A through the production desktop service interface.
            allowed = self.service.resolve_transfer(transfer_id, "allow_receive")
            self.assertTrue(allowed.get("ok"), allowed)
            self.assertEqual("receiving", allowed["transfer"]["state"])

            chunk_zero = file_content[: 1024 * 1024]
            chunk_zero_headers, _, _ = self._headers(
                "PUT",
                f"/api/v1/transfers/uploads/{transfer_id}/chunks/0",
                chunk_zero,
            )
            chunk_zero_response = await client.put(
                f"{self.base_url}/api/v1/transfers/uploads/{transfer_id}/chunks/0",
                content=chunk_zero,
                headers=chunk_zero_headers,
            )
            # Deliberately ignore the authenticated acknowledgement, as a lost
            # response would be on the phone.
            self._verify(chunk_zero_response, "upload_chunk", 200)

            # A reconnect must reconcile the committed index instead of resending.
            status_headers, _, _ = self._headers(
                "GET",
                f"/api/v1/transfers/uploads/{transfer_id}",
                b"",
            )
            status_response = await client.get(
                f"{self.base_url}/api/v1/transfers/uploads/{transfer_id}",
                headers=status_headers,
            )
            status_payload = self._verify(status_response, "upload_status", 200)
            self.assertEqual(1, status_payload["next_chunk_index"])
            self.assertEqual(len(chunk_zero), status_payload["received_bytes"])
            self.assertEqual("receiving", status_payload["state"])

            chunk_one = file_content[1024 * 1024 :]
            self.assertEqual(12_345, len(chunk_one))
            chunk_one_headers, _, _ = self._headers(
                "PUT",
                f"/api/v1/transfers/uploads/{transfer_id}/chunks/1",
                chunk_one,
            )
            chunk_one_response = await client.put(
                f"{self.base_url}/api/v1/transfers/uploads/{transfer_id}/chunks/1",
                content=chunk_one,
                headers=chunk_one_headers,
            )
            chunk_one_payload = self._verify(
                chunk_one_response, "upload_chunk", 200
            )
            self.assertEqual("awaiting_final_acceptance", chunk_one_payload["state"])
            self.assertEqual(file_size, chunk_one_payload["received_bytes"])

            transfers = self.service.get_transfers()
            self.assertTrue(transfers.get("ok"), transfers)
            self.assertEqual(1, len(transfers["transfers"]))
            public = transfers["transfers"][0]
            self.assertEqual("awaiting_final_acceptance", public["state"])
            # The IPC snapshot is path-free: a MIME type legitimately contains
            # "/", but no field may carry a filesystem path or source URI.
            for key, value in public.items():
                if isinstance(value, str):
                    self.assertFalse(value.startswith("/"), f"{key} leaked an absolute path")
                    self.assertNotIn(self.tmp.name, value, f"{key} leaked the test root")
                    self.assertNotIn("content://", value, f"{key} leaked a source URI")
                    self.assertNotIn("file://", value, f"{key} leaked a file URI")
            self.assertEqual("application/pdf", public["mime"])
            self.assertNotIn("path", public)
            self.assertNotIn("source_uri", public)

            downloads = Path(self.tmp.name) / "Downloads"
            self.assertEqual([], list(downloads.iterdir()))

            # Gate B through the production desktop service interface.
            accepted = self.service.resolve_transfer(transfer_id, "accept_file")
            self.assertTrue(accepted.get("ok"), accepted)
            self.assertEqual("committed", accepted["transfer"]["state"])
            published_name = accepted["transfer"]["published_filename"]
            self.assertTrue(published_name)
            published = downloads / published_name
            self.assertTrue(published.is_file())
            self.assertEqual(file_content, published.read_bytes())

    async def test_android_signed_cancel_is_honoured_before_approval(self):
        content = b"cancel me from the phone"
        body = json.dumps(
            {
                "filename": "cancel_me.txt",
                "mime": "text/plain",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        async with httpx.AsyncClient(verify=self.ssl_context) as client:
            headers, _, _ = self._headers(
                "POST", "/api/v1/transfers/uploads/init", body
            )
            init_response = await client.post(
                f"{self.base_url}/api/v1/transfers/uploads/init",
                content=body,
                headers=headers,
            )
            init_payload = self._verify(init_response, "upload_init", 202)
            transfer_id = init_payload["transfer_id"]

            cancel_headers, _, _ = self._headers(
                "POST",
                f"/api/v1/transfers/uploads/{transfer_id}/cancel",
                b"",
            )
            cancel_response = await client.post(
                f"{self.base_url}/api/v1/transfers/uploads/{transfer_id}/cancel",
                headers=cancel_headers,
            )
            cancel_payload = self._verify(cancel_response, "upload_cancel", 200)
            self.assertEqual("cancelled", cancel_payload["state"])

            # An old sequence cannot be replayed. The failure is authenticated
            # (the request MAC is valid), so the desktop returns a signed 409
            # envelope rather than an unsigned 401.
            replay_headers, _, _ = self._headers(
                "GET", f"/api/v1/transfers/uploads/{transfer_id}", b"", sequence=2
            )
            replay_response = await client.get(
                f"{self.base_url}/api/v1/transfers/uploads/{transfer_id}",
                headers=replay_headers,
            )
            self.assertEqual(409, replay_response.status_code)
            envelope = replay_response.json()
            self.assertIn("sig", envelope)
            replay_payload = self._verify(replay_response, "upload_status", 409)
            self.assertEqual("replay_sequence_invalid", replay_payload["error_code"])

            # A request that cannot prove possession of K_up is rejected before
            # any signed envelope is produced, so the client must treat it as a
            # transport/authentication failure rather than protocol state.
            bad_headers, _, _ = self._headers(
                "GET", f"/api/v1/transfers/uploads/{transfer_id}", b""
            )
            bad_headers["X-Kivo-Signature"] = "0" * 64
            bad_response = await client.get(
                f"{self.base_url}/api/v1/transfers/uploads/{transfer_id}",
                headers=bad_headers,
            )
            self.assertEqual(401, bad_response.status_code)
            self.assertNotIn("sig", bad_response.json())


if __name__ == "__main__":
    unittest.main()
