"""Loopback TLS 1.3 tests for authenticated desktop-to-Android downloads."""
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
    generate_ephemeral_certificate,
)
from kivo.bridge.download import DownloadManager, DownloadState
from kivo.bridge.download_validation import DOWNLOAD_CHUNK_BYTES
from kivo.bridge.models import (
    ALGORITHM_ID,
    PAIRING_PROTOCOL_VERSION,
    PairedSession,
)
from kivo.bridge.net import find_available_port
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
from kivo.shelf.path_policy import PathAuthorizer
from kivo.shelf.service import FileShelfService


class BridgeDownloadServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.lan_ip = "127.0.0.1"
        self.port = find_available_port(self.lan_ip, preferred=19545)

        self.cert, self.key, self.cert_fp, self.cert_pem, self.key_pem = (
            generate_ephemeral_certificate(self.lan_ip)
        )
        self.pairing_manager = PairingManager()
        self.quarantine = QuarantineManager(
            runtime_base=os.path.join(self.tmp.name, "runtime"),
            cache_base=os.path.join(self.tmp.name, "cache"),
            downloads_dir=os.path.join(self.tmp.name, "Downloads"),
            free_space_fn=lambda root: 20 * 1024**3,
        )
        for name in ("Downloads", "runtime", "cache"):
            os.makedirs(os.path.join(self.tmp.name, name), exist_ok=True)

        self.transfer_manager = TransferManager(
            session_provider=lambda: self.pairing_manager.active_session,
            quarantine=self.quarantine,
        )
        self.transfer_manager.start()
        self.download_manager = DownloadManager(
            session_provider=lambda: self.pairing_manager.active_session,
        )
        self.download_manager.start()

        self.shelf = FileShelfService(
            authorizer=PathAuthorizer(), xdg_open_path="/bin/true"
        )
        self.shelf.start()
        self.data = bytes(index % 253 for index in range(DOWNLOAD_CHUNK_BYTES + 777))
        self.file_path = self.root / "download-source.bin"
        self.file_path.write_bytes(self.data)
        added, rejected = self.shelf.add_items([self.file_path.as_uri()])
        self.assertEqual(rejected, [])
        self.item_id = added[0].id

        self.server = BridgeServer(
            pairing_manager=self.pairing_manager,
            lan_ip=self.lan_ip,
            port=self.port,
            cert_pem=self.cert_pem,
            key_pem=self.key_pem,
            transfer_manager=self.transfer_manager,
            download_manager=self.download_manager,
        )
        self.server.start()
        await asyncio.sleep(0.3)

        self.client_ssl = ssl.create_default_context()
        self.client_ssl.load_verify_locations(cadata=self.cert_pem.decode("ascii"))

    async def asyncTearDown(self) -> None:
        self.download_manager.close()
        self.transfer_manager.close()
        self.shelf.close()
        self.server.stop(timeout=2.0)
        self.tmp.cleanup()
        await asyncio.sleep(0.1)

    async def _establish_paired_session(self) -> PairedSession:
        offer = self.pairing_manager.create_offer(self.lan_ip, self.port, self.cert_fp)
        client_nonce = os.urandom(32)
        context = build_canonical_context(
            version=PAIRING_PROTOCOL_VERSION,
            session_id=offer.session_id,
            cert_fp=offer.cert_fingerprint,
            server_nonce=offer.server_nonce,
            client_nonce=client_nonce,
            client_id="client_download_test",
            algorithm_id=ALGORITHM_ID,
        )
        proof = compute_client_proof(offer.pairing_secret, context)
        payload = {
            "version": PAIRING_PROTOCOL_VERSION,
            "session_id": offer.session_id,
            "client_id": "client_download_test",
            "device_name": "Download Test Phone",
            "client_nonce_b64": base64.urlsafe_b64encode(client_nonce).decode("ascii").rstrip("="),
            "client_proof_b64": base64.urlsafe_b64encode(proof).decode("ascii").rstrip("="),
        }
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            task = asyncio.create_task(
                client.post(
                    f"https://{self.lan_ip}:{self.port}/api/v1/pair/handshake",
                    json=payload,
                    timeout=10.0,
                )
            )
            await asyncio.sleep(0.2)
            self.pairing_manager.approve_pairing(offer.session_id)
            response = await task
            self.assertEqual(response.status_code, 200)
        session = self.pairing_manager.active_session
        self.assertIsNotNone(session)
        return session

    def _issue_ticket(self, filename: str = "download-source.bin"):
        path = self.root / filename
        if not path.exists():
            path.write_bytes(self.data)
            added, rejected = self.shelf.add_items([path.as_uri()])
            self.assertEqual(rejected, [])
            item_id = added[0].id
        else:
            item_id = self.item_id
        return self.download_manager.create_ticket(
            self.shelf.authorize_export(item_id)
        )

    def _headers(
        self,
        session: PairedSession,
        *,
        method: str,
        endpoint: str,
        sequence: int,
        content: bytes = b"",
        timestamp: float | None = None,
        tampered_sig: bool = False,
        sign_endpoint: str | None = None,
    ) -> dict[str, str]:
        nonce = os.urandom(16)
        ts = timestamp if timestamp is not None else time.time()
        content_sha256 = hashlib.sha256(content).digest()
        canonical = build_canonical_transfer_request(
            version=TRANSFER_PROTOCOL_VERSION,
            session_id=session.session_id,
            method=method,
            endpoint=sign_endpoint or endpoint,
            sequence=sequence,
            nonce=nonce,
            timestamp=ts,
            content_length=len(content),
            content_sha256=content_sha256,
        )
        signature = compute_transfer_request_signature(session.upstream_key, canonical)
        if tampered_sig:
            signature = bytes([signature[0] ^ 0xFF]) + signature[1:]
        return {
            "Authorization": f"Bearer {session.bearer_token}",
            "X-Kivo-Protocol": TRANSFER_PROTOCOL_VERSION,
            "X-Kivo-Session": session.session_id,
            "X-Kivo-Sequence": str(sequence),
            "X-Kivo-Nonce": encode_nonce_b64(nonce),
            "X-Kivo-Timestamp": str(ts),
            "X-Kivo-Content-SHA256": content_sha256.hex(),
            "X-Kivo-Signature": signature.hex(),
            "Content-Length": str(len(content)),
        }

    def _verify_signed(
        self,
        session: PairedSession,
        response: httpx.Response,
        context: str,
        status: int,
    ) -> dict:
        body = response.json()
        self.assertEqual(body.get("v"), TRANSFER_PROTOCOL_VERSION)
        self.assertEqual(body.get("ctx"), context)
        self.assertEqual(body.get("sid"), session.session_id)
        self.assertEqual(body.get("http_status"), status)
        payload_bytes = base64.b64decode(body.get("payload_b64", ""))
        self.assertEqual(
            hashlib.sha256(payload_bytes).hexdigest(), body.get("payload_sha256")
        )
        canonical = build_canonical_transfer_response(
            version=TRANSFER_PROTOCOL_VERSION,
            response_context=context,
            session_id=session.session_id,
            http_status=status,
            sequence=body.get("seq"),
            nonce=base64.urlsafe_b64decode(body.get("nonce_b64") + "=="),
            state=body.get("state"),
            transfer_id=body.get("tid"),
            payload_length=len(payload_bytes),
            payload_sha256=bytes.fromhex(body.get("payload_sha256")),
        )
        self.assertTrue(
            verify_transfer_response_signature(
                session.downstream_key, canonical, bytes.fromhex(body.get("sig", ""))
            )
        )
        self.assertNotIn(str(self.root), json.dumps(body))
        self.assertNotIn(self.item_id, json.dumps(body))
        return json.loads(payload_bytes.decode("utf-8"))

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        endpoint: str,
        headers: dict[str, str],
        content: bytes = b"",
    ) -> httpx.Response:
        return await client.request(
            method, f"https://{self.lan_ip}:{self.port}{endpoint}", content=content, headers=headers
        )

    async def test_full_download_flow(self):
        session = await self._establish_paired_session()
        ticket = self._issue_ticket()
        body = json.dumps({"ticket_id": ticket.download_id}).encode("utf-8")

        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            response = await self._request(
                client,
                "POST",
                "/api/v1/transfers/downloads/init",
                self._headers(
                    session,
                    method="POST",
                    endpoint="/api/v1/transfers/downloads/init",
                    sequence=1,
                    content=body,
                ),
                body,
            )
            self.assertEqual(response.status_code, 200)
            init_payload = self._verify_signed(session, response, "download_init", 200)
            self.assertEqual(init_payload["state"], "claimed")
            self.assertEqual(init_payload["download_id"], ticket.download_id)
            self.assertEqual(init_payload["sha256"], hashlib.sha256(self.data).hexdigest())

            status_response = await self._request(
                client,
                "GET",
                f"/api/v1/transfers/downloads/{ticket.download_id}",
                self._headers(
                    session,
                    method="GET",
                    endpoint=f"/api/v1/transfers/downloads/{ticket.download_id}",
                    sequence=2,
                ),
            )
            self.assertEqual(status_response.status_code, 200)
            status_payload = self._verify_signed(
                session, status_response, "download_status", 200
            )
            self.assertEqual(status_payload["state"], "claimed")

            received: list[bytes] = []
            for index in range(ticket.total_chunks):
                endpoint = f"/api/v1/transfers/downloads/{ticket.download_id}/chunks/{index}"
                chunk_response = await self._request(
                    client,
                    "GET",
                    endpoint,
                    self._headers(
                        session,
                        method="GET",
                        endpoint=endpoint,
                        sequence=3 + index,
                    ),
                )
                self.assertEqual(chunk_response.status_code, 200)
                payload = self._verify_signed(
                    session, chunk_response, "download_chunk", 200
                )
                chunk = base64.b64decode(payload["chunk_b64"])
                self.assertEqual(payload["chunk_index"], index)
                self.assertEqual(payload["offset"], index * DOWNLOAD_CHUNK_BYTES)
                self.assertEqual(payload["length"], len(chunk))
                self.assertEqual(payload["chunk_sha256"], hashlib.sha256(chunk).hexdigest())
                received.append(chunk)

            self.assertEqual(b"".join(received), self.data)
            complete_endpoint = (
                f"/api/v1/transfers/downloads/{ticket.download_id}/complete"
            )
            complete_response = await self._request(
                client,
                "POST",
                complete_endpoint,
                self._headers(
                    session,
                    method="POST",
                    endpoint=complete_endpoint,
                    sequence=20,
                ),
            )
            self.assertEqual(complete_response.status_code, 200)
            complete_payload = self._verify_signed(
                session, complete_response, "download_complete", 200
            )
            self.assertEqual(complete_payload["state"], "completed")
            self.assertEqual(complete_payload["bytes_sent"], len(self.data))

    async def test_replay_nonce_is_rejected_with_signed_409(self):
        session = await self._establish_paired_session()
        ticket = self._issue_ticket()
        body = json.dumps({"ticket_id": ticket.download_id}).encode("utf-8")
        headers = self._headers(
            session,
            method="POST",
            endpoint="/api/v1/transfers/downloads/init",
            sequence=1,
            content=body,
        )
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            first = await self._request(
                client, "POST", "/api/v1/transfers/downloads/init", headers, body
            )
            self.assertEqual(first.status_code, 200)
            replay = await self._request(
                client, "POST", "/api/v1/transfers/downloads/init", headers, body
            )
            self.assertEqual(replay.status_code, 409)
            payload = self._verify_signed(session, replay, "download_init", 409)
            self.assertIn(
                payload["error_code"],
                ("replay_nonce_detected", "replay_sequence_invalid"),
            )

    async def test_missing_and_tampered_authentication_is_unsigned_401(self):
        session = await self._establish_paired_session()
        ticket = self._issue_ticket()
        endpoint = f"/api/v1/transfers/downloads/{ticket.download_id}"
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            missing = await client.get(
                f"https://{self.lan_ip}:{self.port}{endpoint}"
            )
            self.assertEqual(missing.status_code, 401)
            self.assertNotIn("sig", missing.json())

            tampered = await self._request(
                client,
                "GET",
                endpoint,
                self._headers(
                    session,
                    method="GET",
                    endpoint=endpoint,
                    sequence=1,
                    tampered_sig=True,
                ),
            )
            self.assertEqual(tampered.status_code, 401)
            self.assertNotIn("sig", tampered.json())

            wrong_route = await self._request(
                client,
                "GET",
                endpoint,
                self._headers(
                    session,
                    method="GET",
                    endpoint=endpoint,
                    sequence=2,
                    sign_endpoint="/api/v1/transfers/uploads/init",
                ),
            )
            self.assertEqual(wrong_route.status_code, 401)
            self.assertNotIn("sig", wrong_route.json())

    async def test_stale_timestamp_is_rejected(self):
        session = await self._establish_paired_session()
        ticket = self._issue_ticket()
        endpoint = f"/api/v1/transfers/downloads/{ticket.download_id}"
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            response = await self._request(
                client,
                "GET",
                endpoint,
                self._headers(
                    session,
                    method="GET",
                    endpoint=endpoint,
                    sequence=1,
                    timestamp=time.time() - 120.0,
                ),
            )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("sig", response.json())

    async def test_init_body_is_bound_to_ticket_id(self):
        session = await self._establish_paired_session()
        first = self._issue_ticket()
        second = self._issue_ticket("second-source.bin")
        signed_body = json.dumps({"ticket_id": first.download_id}).encode("utf-8")
        sent_body = json.dumps({"ticket_id": second.download_id}).encode("utf-8")
        self.assertEqual(len(signed_body), len(sent_body))
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            response = await self._request(
                client,
                "POST",
                "/api/v1/transfers/downloads/init",
                self._headers(
                    session,
                    method="POST",
                    endpoint="/api/v1/transfers/downloads/init",
                    sequence=1,
                    content=signed_body,
                ),
                sent_body,
            )
        self.assertEqual(response.status_code, 400)
        payload = self._verify_signed(session, response, "download_init", 400)
        self.assertEqual(payload["error_code"], "body_hash_mismatch")

    async def test_chunk_index_is_bound_to_request_path(self):
        session = await self._establish_paired_session()
        ticket = self._issue_ticket()
        init_body = json.dumps({"ticket_id": ticket.download_id}).encode("utf-8")
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            init = await self._request(
                client,
                "POST",
                "/api/v1/transfers/downloads/init",
                self._headers(
                    session,
                    method="POST",
                    endpoint="/api/v1/transfers/downloads/init",
                    sequence=1,
                    content=init_body,
                ),
                init_body,
            )
            self.assertEqual(init.status_code, 200)

            chunk_zero = f"/api/v1/transfers/downloads/{ticket.download_id}/chunks/0"
            headers = self._headers(
                session, method="GET", endpoint=chunk_zero, sequence=2
            )
            chunk_one = f"/api/v1/transfers/downloads/{ticket.download_id}/chunks/1"
            response = await self._request(client, "GET", chunk_one, headers)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("sig", response.json())

    async def test_out_of_order_and_incomplete_are_signed_409(self):
        session = await self._establish_paired_session()
        ticket = self._issue_ticket()
        init_body = json.dumps({"ticket_id": ticket.download_id}).encode("utf-8")
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            await self._request(
                client,
                "POST",
                "/api/v1/transfers/downloads/init",
                self._headers(
                    session,
                    method="POST",
                    endpoint="/api/v1/transfers/downloads/init",
                    sequence=1,
                    content=init_body,
                ),
                init_body,
            )
            chunk_one = f"/api/v1/transfers/downloads/{ticket.download_id}/chunks/1"
            out_of_order = await self._request(
                client,
                "GET",
                chunk_one,
                self._headers(session, method="GET", endpoint=chunk_one, sequence=2),
            )
            self.assertEqual(out_of_order.status_code, 409)
            payload = self._verify_signed(
                session, out_of_order, "download_chunk", 409
            )
            self.assertEqual(payload["error_code"], "chunk_out_of_order")

            complete = f"/api/v1/transfers/downloads/{ticket.download_id}/complete"
            incomplete = await self._request(
                client,
                "POST",
                complete,
                self._headers(session, method="POST", endpoint=complete, sequence=3),
            )
            self.assertEqual(incomplete.status_code, 409)
            payload = self._verify_signed(
                session, incomplete, "download_complete", 409
            )
            self.assertEqual(payload["error_code"], "download_incomplete")

    async def test_cancel_then_chunk_is_rejected(self):
        session = await self._establish_paired_session()
        ticket = self._issue_ticket()
        init_body = json.dumps({"ticket_id": ticket.download_id}).encode("utf-8")
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            await self._request(
                client,
                "POST",
                "/api/v1/transfers/downloads/init",
                self._headers(
                    session,
                    method="POST",
                    endpoint="/api/v1/transfers/downloads/init",
                    sequence=1,
                    content=init_body,
                ),
                init_body,
            )
            cancel = f"/api/v1/transfers/downloads/{ticket.download_id}/cancel"
            response = await self._request(
                client,
                "POST",
                cancel,
                self._headers(session, method="POST", endpoint=cancel, sequence=2),
            )
            self.assertEqual(response.status_code, 200)
            payload = self._verify_signed(session, response, "download_cancel", 200)
            self.assertEqual(payload["state"], "cancelled")

            chunk = f"/api/v1/transfers/downloads/{ticket.download_id}/chunks/0"
            rejected = await self._request(
                client,
                "GET",
                chunk,
                self._headers(session, method="GET", endpoint=chunk, sequence=3),
            )
            self.assertEqual(rejected.status_code, 409)
            payload = self._verify_signed(session, rejected, "download_chunk", 409)
            self.assertEqual(payload["error_code"], "invalid_download_state")

    async def test_zero_length_download_completes(self):
        session = await self._establish_paired_session()
        empty = self.root / "empty.bin"
        empty.write_bytes(b"")
        added, rejected = self.shelf.add_items([empty.as_uri()])
        self.assertEqual(rejected, [])
        ticket = self.download_manager.create_ticket(
            self.shelf.authorize_export(added[0].id)
        )
        init_body = json.dumps({"ticket_id": ticket.download_id}).encode("utf-8")
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            await self._request(
                client,
                "POST",
                "/api/v1/transfers/downloads/init",
                self._headers(
                    session,
                    method="POST",
                    endpoint="/api/v1/transfers/downloads/init",
                    sequence=1,
                    content=init_body,
                ),
                init_body,
            )
            complete = f"/api/v1/transfers/downloads/{ticket.download_id}/complete"
            response = await self._request(
                client,
                "POST",
                complete,
                self._headers(session, method="POST", endpoint=complete, sequence=2),
            )
            self.assertEqual(response.status_code, 200)
            payload = self._verify_signed(session, response, "download_complete", 200)
            self.assertEqual(payload["state"], "completed")
            self.assertEqual(payload["bytes_sent"], 0)

    async def test_end_session_revokes_outstanding_ticket(self):
        session = await self._establish_paired_session()
        ticket = self._issue_ticket()
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            response = await client.delete(
                f"https://{self.lan_ip}:{self.port}/api/v1/session",
                headers={"Authorization": f"Bearer {session.bearer_token}"},
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(ticket.state, DownloadState.REVOKED)
        self.assertIsNone(ticket.grant)
        status = self.download_manager.get_by_id(ticket.download_id)
        self.assertEqual(status.state, DownloadState.REVOKED)

    async def test_second_device_cannot_claim_ticket(self):
        first_session = await self._establish_paired_session()
        ticket = self._issue_ticket()
        self.pairing_manager.revoke_device(first_session.session_id)
        self.download_manager.cancel_session(first_session.session_id)
        second_session = await self._establish_paired_session()
        body = json.dumps({"ticket_id": ticket.download_id}).encode("utf-8")
        async with httpx.AsyncClient(verify=self.client_ssl) as client:
            response = await self._request(
                client,
                "POST",
                "/api/v1/transfers/downloads/init",
                self._headers(
                    second_session,
                    method="POST",
                    endpoint="/api/v1/transfers/downloads/init",
                    sequence=1,
                    content=body,
                ),
                body,
            )
        self.assertEqual(response.status_code, 403)
        payload = self._verify_signed(session=second_session, response=response, context="download_init", status=403)
        self.assertEqual(payload["error_code"], "download_forbidden")
        # An unrelated paired device must not learn the ticket's file metadata.
        for forbidden in ("filename", "sha256", "size", "bytes_sent", "state"):
            self.assertNotIn(forbidden, payload)
        self.assertNotIn("download-source", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
