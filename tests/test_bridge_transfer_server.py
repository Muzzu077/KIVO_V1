"""Loopback TLS 1.3 integration and adversarial tests for bridge transfer endpoints."""
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

import httpx

from kivo.bridge.crypto import (
    build_canonical_context,
    compute_client_proof,
    derive_sas_code,
    derive_session_keys,
    generate_ephemeral_certificate,
)
from kivo.bridge.models import (
    ALGORITHM_ID,
    PAIRING_PROTOCOL_VERSION,
    PairedSession,
)
from kivo.bridge.pairing import PairingManager
from kivo.bridge.quarantine import QuarantineManager
from kivo.bridge.server import BridgeServer
from kivo.bridge.transfer import TransferManager
from kivo.bridge.transfer_crypto import (
    TRANSFER_PROTOCOL_VERSION,
    build_canonical_transfer_request,
    build_canonical_transfer_response,
    compute_transfer_request_signature,
    encode_nonce_b64,
    verify_transfer_response_signature,
)


class BridgeTransferServerTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.lan_ip = "127.0.0.1"
        self.port = 19443

        self.cert, self.key, self.cert_fp, self.cert_pem, self.key_pem = (
            generate_ephemeral_certificate(self.lan_ip)
        )

        self.pairing_manager = PairingManager()
        self.quarantine = QuarantineManager(
            runtime_base=os.path.join(self.tmp.name, "runtime"),
            cache_base=os.path.join(self.tmp.name, "cache"),
            downloads_dir=os.path.join(self.tmp.name, "Downloads"),
            free_space_fn=lambda r: 20 * 1024**3,
        )
        os.makedirs(os.path.join(self.tmp.name, "Downloads"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp.name, "runtime"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp.name, "cache"), exist_ok=True)

        self.transfer_manager = TransferManager(
            session_provider=lambda: self.pairing_manager.active_session,
            quarantine=self.quarantine,
        )
        self.transfer_manager.start()

        self.server = BridgeServer(
            pairing_manager=self.pairing_manager,
            lan_ip=self.lan_ip,
            port=self.port,
            cert_pem=self.cert_pem,
            key_pem=self.key_pem,
            transfer_manager=self.transfer_manager,
        )
        self.server.start()
        await asyncio.sleep(0.3)

        self.client_ssl = ssl.create_default_context()
        self.client_ssl.load_verify_locations(cadata=self.cert_pem.decode("ascii"))

    async def asyncTearDown(self) -> None:
        self.transfer_manager.close()
        self.server.stop(timeout=2.0)
        self.tmp.cleanup()
        await asyncio.sleep(0.1)

    async def _establish_paired_session(self) -> PairedSession:
        offer = self.pairing_manager.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        client_id = "client_transfer_test"
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

        payload = {
            "version": PAIRING_PROTOCOL_VERSION,
            "session_id": offer.session_id,
            "client_id": client_id,
            "device_name": device_name,
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
            await asyncio.sleep(0.2)
            self.pairing_manager.approve_pairing(offer.session_id)
            resp = await handshake_task
            self.assertEqual(resp.status_code, 200)

        session = self.pairing_manager.active_session
        self.assertIsNotNone(session)
        return session

    def _build_auth_headers(
        self,
        session: PairedSession,
        *,
        method: str,
        endpoint: str,
        sequence: int,
        content: bytes,
        timestamp: float | None = None,
        tampered_sig: bool = False,
    ) -> dict[str, str]:
        nonce = os.urandom(16)
        ts = timestamp if timestamp is not None else time.time()
        content_sha256 = hashlib.sha256(content).digest()
        canonical = build_canonical_transfer_request(
            version=TRANSFER_PROTOCOL_VERSION,
            session_id=session.session_id,
            method=method,
            endpoint=endpoint,
            sequence=sequence,
            nonce=nonce,
            timestamp=ts,
            content_length=len(content),
            content_sha256=content_sha256,
        )
        sig = compute_transfer_request_signature(session.upstream_key, canonical)
        if tampered_sig:
            sig = bytes([sig[0] ^ 0xFF]) + sig[1:]
        return {
            "Authorization": f"Bearer {session.bearer_token}",
            "X-Kivo-Protocol": TRANSFER_PROTOCOL_VERSION,
            "X-Kivo-Session": session.session_id,
            "X-Kivo-Sequence": str(sequence),
            "X-Kivo-Nonce": encode_nonce_b64(nonce),
            "X-Kivo-Timestamp": str(ts),
            "X-Kivo-Content-SHA256": content_sha256.hex(),
            "X-Kivo-Signature": sig.hex(),
            "Content-Length": str(len(content)),
        }

    def _verify_signed_response(
        self,
        session: PairedSession,
        resp_json: dict,
        expected_context: str,
        expected_status: int,
    ) -> dict:
        self.assertEqual(resp_json.get("v"), TRANSFER_PROTOCOL_VERSION)
        self.assertEqual(resp_json.get("ctx"), expected_context)
        self.assertEqual(resp_json.get("sid"), session.session_id)
        self.assertEqual(resp_json.get("http_status"), expected_status)

        payload_bytes = base64.b64decode(resp_json.get("payload_b64", ""))
        self.assertEqual(hashlib.sha256(payload_bytes).hexdigest(), resp_json.get("payload_sha256"))

        canonical = build_canonical_transfer_response(
            version=TRANSFER_PROTOCOL_VERSION,
            response_context=expected_context,
            session_id=session.session_id,
            http_status=expected_status,
            sequence=resp_json.get("seq"),
            nonce=base64.urlsafe_b64decode(resp_json.get("nonce_b64") + "=="),
            state=resp_json.get("state"),
            transfer_id=resp_json.get("tid"),
            payload_length=len(payload_bytes),
            payload_sha256=bytes.fromhex(resp_json.get("payload_sha256")),
        )
        sig = bytes.fromhex(resp_json.get("sig", ""))
        self.assertTrue(
            verify_transfer_response_signature(session.downstream_key, canonical, sig)
        )
        return json.loads(payload_bytes.decode("utf-8"))

    # ── Upload Initiation ──────────────────────────────────────────────────

    async def test_upload_init_success(self):
        session = await self._establish_paired_session()
        data = b"sample content"
        body = json.dumps({
            "filename": "document.pdf",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mime": "application/pdf",
        }).encode("utf-8")

        headers = self._build_auth_headers(
            session,
            method="POST",
            endpoint="/api/v1/transfers/uploads/init",
            sequence=1,
            content=body,
        )

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            resp = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/transfers/uploads/init",
                content=body,
                headers=headers,
            )
            self.assertEqual(resp.status_code, 202)
            payload = self._verify_signed_response(session, resp.json(), "upload_init", 202)
            self.assertEqual(payload.get("filename"), "document.pdf")
            self.assertEqual(payload.get("state"), "awaiting_desktop_approval")
            self.assertTrue(payload.get("transfer_id").startswith("tr_"))

    async def test_upload_init_tampered_signature(self):
        session = await self._establish_paired_session()
        body = json.dumps({
            "filename": "doc.pdf", "size": 10,
            "sha256": hashlib.sha256(b"x" * 10).hexdigest(),
        }).encode("utf-8")

        headers = self._build_auth_headers(
            session,
            method="POST",
            endpoint="/api/v1/transfers/uploads/init",
            sequence=1,
            content=body,
            tampered_sig=True,
        )

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            resp = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/transfers/uploads/init",
                content=body,
                headers=headers,
            )
            self.assertEqual(resp.status_code, 401)
            data = resp.json()
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("error_code"), "invalid_signature")

    async def test_upload_init_stale_timestamp(self):
        session = await self._establish_paired_session()
        body = json.dumps({
            "filename": "doc.pdf", "size": 10,
            "sha256": hashlib.sha256(b"x" * 10).hexdigest(),
        }).encode("utf-8")

        headers = self._build_auth_headers(
            session,
            method="POST",
            endpoint="/api/v1/transfers/uploads/init",
            sequence=1,
            content=body,
            timestamp=time.time() - 300.0,
        )

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            resp = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/transfers/uploads/init",
                content=body,
                headers=headers,
            )
            self.assertEqual(resp.status_code, 401)
            self.assertEqual(resp.json().get("error_code"), "timestamp_out_of_bounds")

    async def test_upload_init_replay_rejected(self):
        session = await self._establish_paired_session()
        body = json.dumps({
            "filename": "doc.pdf", "size": 10,
            "sha256": hashlib.sha256(b"x" * 10).hexdigest(),
        }).encode("utf-8")

        headers = self._build_auth_headers(
            session,
            method="POST",
            endpoint="/api/v1/transfers/uploads/init",
            sequence=1,
            content=body,
        )

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            resp1 = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/transfers/uploads/init",
                content=body,
                headers=headers,
            )
            self.assertEqual(resp1.status_code, 202)

            resp2 = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/transfers/uploads/init",
                content=body,
                headers=headers,
            )
            self.assertEqual(resp2.status_code, 409)

    # ── Full End-to-End Transfer ───────────────────────────────────────────

    async def test_end_to_end_upload_flow(self):
        session = await self._establish_paired_session()
        file_content = b"KIVO Stage 13.3.3.1 end-to-end verified upload payload!"
        file_size = len(file_content)
        file_sha256 = hashlib.sha256(file_content).digest()

        # 1. Init upload
        init_body = json.dumps({
            "filename": "e2e_report.pdf",
            "size": file_size,
            "sha256": file_sha256.hex(),
            "mime": "application/pdf",
        }).encode("utf-8")

        headers = self._build_auth_headers(
            session,
            method="POST",
            endpoint="/api/v1/transfers/uploads/init",
            sequence=1,
            content=init_body,
        )

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            init_resp = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/transfers/uploads/init",
                content=init_body,
                headers=headers,
            )
            self.assertEqual(init_resp.status_code, 202)
            init_payload = self._verify_signed_response(session, init_resp.json(), "upload_init", 202)
            transfer_id = init_payload.get("transfer_id")
            self.assertIsNotNone(transfer_id)

            # 2. Gate A: Desktop user approves receive
            self.transfer_manager.resolve(transfer_id, "allow_receive")

            # 3. Stream chunk
            chunk_headers = self._build_auth_headers(
                session,
                method="PUT",
                endpoint=f"/api/v1/transfers/uploads/{transfer_id}/chunks/0",
                sequence=2,
                content=file_content,
            )
            chunk_resp = await client.put(
                f"https://{self.lan_ip}:{self.port}/api/v1/transfers/uploads/{transfer_id}/chunks/0",
                content=file_content,
                headers=chunk_headers,
            )
            self.assertEqual(chunk_resp.status_code, 200)
            chunk_payload = self._verify_signed_response(session, chunk_resp.json(), "upload_chunk", 200)
            self.assertEqual(chunk_payload.get("state"), "awaiting_final_acceptance")

            # 4. Status query while awaiting final acceptance
            status_headers = self._build_auth_headers(
                session,
                method="GET",
                endpoint=f"/api/v1/transfers/uploads/{transfer_id}",
                sequence=3,
                content=b"",
            )
            status_resp = await client.get(
                f"https://{self.lan_ip}:{self.port}/api/v1/transfers/uploads/{transfer_id}",
                headers=status_headers,
            )
            self.assertEqual(status_resp.status_code, 200)
            status_payload = self._verify_signed_response(session, status_resp.json(), "upload_status", 200)
            self.assertEqual(status_payload.get("state"), "awaiting_final_acceptance")

            # 5. Gate B: Desktop user accepts completed file
            resolved = self.transfer_manager.resolve(transfer_id, "accept_file")
            self.assertEqual(resolved.state.value, "committed")
            self.assertIsNotNone(resolved.published_filename)

            # 6. Verify file is in Downloads
            downloads_dir = Path(self.tmp.name) / "Downloads"
            published_path = downloads_dir / resolved.published_filename
            self.assertTrue(published_path.exists())
            self.assertEqual(published_path.read_bytes(), file_content)

    # ── Cancel Flow ────────────────────────────────────────────────────────

    async def test_upload_cancel_endpoint(self):
        session = await self._establish_paired_session()
        data = b"some data"
        init_body = json.dumps({
            "filename": "cancel_me.pdf",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }).encode("utf-8")

        headers = self._build_auth_headers(
            session,
            method="POST",
            endpoint="/api/v1/transfers/uploads/init",
            sequence=1,
            content=init_body,
        )

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            init_resp = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/transfers/uploads/init",
                content=init_body,
                headers=headers,
            )
            transfer_id = init_resp.json().get("tid")

            cancel_headers = self._build_auth_headers(
                session,
                method="POST",
                endpoint=f"/api/v1/transfers/uploads/{transfer_id}/cancel",
                sequence=2,
                content=b"",
            )
            cancel_resp = await client.post(
                f"https://{self.lan_ip}:{self.port}/api/v1/transfers/uploads/{transfer_id}/cancel",
                headers=cancel_headers,
            )
            self.assertEqual(cancel_resp.status_code, 200)
            payload = self._verify_signed_response(session, cancel_resp.json(), "upload_cancel", 200)
            self.assertEqual(payload.get("state"), "cancelled")


if __name__ == "__main__":
    unittest.main()
