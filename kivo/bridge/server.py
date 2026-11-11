"""Ephemeral Uvicorn/FastAPI server with TLS 1.3 for Phone Bridge."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import ssl

from kivo.bridge.models import PAIRING_APPROVAL_TTL_SECONDS, PAIRING_PROTOCOL_VERSION
from kivo.bridge.download import (
    DownloadChunk,
    DownloadError,
    DownloadManager,
    public_download,
)
from kivo.bridge.download_validation import MAX_CONTROL_BODY_BYTES as MAX_DOWNLOAD_CONTROL_BODY_BYTES
from kivo.bridge.pairing import PairingManager
from kivo.bridge.transfer import (
    TransferAuth,
    TransferError,
    TransferManager,
    public_transfer,
)
from kivo.bridge.transfer_crypto import (
    TRANSFER_PROTOCOL_VERSION,
    build_canonical_transfer_response,
    compute_transfer_response_signature,
    encode_nonce_b64,
)
from kivo.bridge.transfer_validation import (
    MAX_CONTROL_BODY_BYTES,
    TRANSFER_CHUNK_BYTES,
    TRANSFER_RECEIVE_TIMEOUT_SECONDS,
    UploadValidationError,
    validate_mime_hint,
    validate_sha256_hex,
)


class HandshakePayload(BaseModel):
    version: str = Field(default=PAIRING_PROTOCOL_VERSION, max_length=8)
    session_id: str = Field(..., min_length=1, max_length=64)
    client_id: str = Field(..., min_length=1, max_length=64)
    device_name: str = Field(..., min_length=1, max_length=64)
    client_nonce_b64: str = Field(..., min_length=16, max_length=128)
    client_proof_b64: str = Field(..., min_length=16, max_length=128)


class PairingStatusPayload(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    sequence: int = Field(..., ge=1, le=2**63 - 1)
    request_nonce_b64: str = Field(..., min_length=16, max_length=128)
    timestamp: float = Field(..., ge=0, le=4102444800.0, allow_inf_nan=False)
    signature_hex: str = Field(..., min_length=64, max_length=64)


def _hex_to_bytes(value: object, length: int) -> bytes | None:
    if not isinstance(value, str) or len(value) != length * 2:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


class BridgeServer:
    """Ephemeral server hosting the local authenticated REST/WebSocket endpoints."""

    def __init__(
        self,
        pairing_manager: PairingManager,
        lan_ip: str,
        port: int,
        cert_pem: bytes,
        key_pem: bytes,
        on_pairing_requested: Callable[[dict[str, Any]], None] | None = None,
        transfer_manager: TransferManager | None = None,
        download_manager: DownloadManager | None = None,
    ):
        self._pairing_manager = pairing_manager
        self._lan_ip = lan_ip
        self._port = port
        self._cert_pem = cert_pem
        self._key_pem = key_pem
        self._on_pairing_requested = on_pairing_requested
        self._transfer_manager = transfer_manager
        self._download_manager = download_manager

        self._app = FastAPI(title="KIVO Phone Bridge", docs_url=None, redoc_url=None)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._temp_dir: tempfile.TemporaryDirectory | None = None

        self._setup_routes()

    def _setup_routes(self) -> None:
        app = self._app

        @app.post("/api/v1/pair/handshake")
        async def handshake(req_body: HandshakePayload, request: Request):
            client_ip = request.client.host if request.client else "unknown"

            # 1. Protocol version validation
            if req_body.version != PAIRING_PROTOCOL_VERSION:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="unsupported_protocol_version",
                )

            # 2. Strict base64 decoding and length validation
            try:
                c_nonce = base64.urlsafe_b64decode(req_body.client_nonce_b64 + "==")
                c_proof = base64.urlsafe_b64decode(req_body.client_proof_b64 + "==")
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid_base64_encoding",
                )

            if len(c_nonce) != 32:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid_nonce_length",
                )

            if len(c_proof) != 32:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid_proof_length",
                )

            ok, err_code, sas_code = self._pairing_manager.handle_client_handshake(
                session_id=req_body.session_id,
                client_id=req_body.client_id,
                device_name=req_body.device_name,
                client_ip=client_ip,
                client_nonce=c_nonce,
                client_proof=c_proof,
                version=req_body.version,
            )

            if not ok:
                if err_code in ("rate_limited", "too_many_failed_attempts"):
                    status_code = status.HTTP_429_TOO_MANY_REQUESTS
                elif err_code == "token_already_consumed":
                    status_code = status.HTTP_409_CONFLICT
                elif err_code == "offer_expired":
                    status_code = status.HTTP_410_GONE
                elif err_code == "no_active_offer":
                    status_code = status.HTTP_404_NOT_FOUND
                elif err_code == "invalid_session_id":
                    status_code = status.HTTP_400_BAD_REQUEST
                elif err_code == "invalid_proof":
                    status_code = status.HTTP_401_UNAUTHORIZED
                else:
                    status_code = status.HTTP_400_BAD_REQUEST
                raise HTTPException(status_code=status_code, detail=err_code)

            # Notify desktop UI that pairing is requested and SAS code is ready
            if self._on_pairing_requested:
                self._on_pairing_requested({
                    "session_id": req_body.session_id,
                    "client_id": req_body.client_id,
                    "device_name": req_body.device_name,
                    "client_ip": client_ip,
                    "sas_code": sas_code,
                })

            # Await desktop user manual approval
            approved = await asyncio.to_thread(
                self._pairing_manager.wait_for_desktop_approval,
                timeout=PAIRING_APPROVAL_TTL_SECONDS,
            )

            if not approved:
                raise HTTPException(
                    status_code=status.HTTP_408_REQUEST_TIMEOUT,
                    detail="Pairing approval timed out or was rejected on desktop",
                )

            session = self._pairing_manager.active_session
            if session is None or session.session_id != req_body.session_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Pairing rejected on desktop",
                )

            signed = self._pairing_manager.build_handshake_response(
                session_id=req_body.session_id,
                client_nonce=c_nonce,
            )
            if signed is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Pairing response could not be authenticated",
                )

            return {"ok": True, **signed}

        @app.post("/api/v1/pair/status")
        async def pair_status(req_body: PairingStatusPayload):
            try:
                nonce_bytes = base64.urlsafe_b64decode(req_body.request_nonce_b64 + "==")
                sig_bytes = bytes.fromhex(req_body.signature_hex)
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid_encoding_or_hex",
                )

            ok, status_name, data = self._pairing_manager.check_authenticated_poll_status(
                session_id=req_body.session_id,
                sequence=req_body.sequence,
                request_nonce=nonce_bytes,
                timestamp=req_body.timestamp,
                signature=sig_bytes,
            )

            if not ok:
                if status_name == "rejected":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="rejected_by_desktop",
                    )
                if status_name in ("expired", "polling_window_closed"):
                    raise HTTPException(
                        status_code=status.HTTP_410_GONE,
                        detail=status_name,
                    )
                if status_name in ("replay_sequence_invalid", "replay_nonce_detected"):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=status_name,
                    )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=status_name,
                )

            if data is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="response_signing_failed",
                )

            return {"ok": True, **data}

        @app.post("/api/v1/transfers/uploads/init")
        async def upload_init(request: Request):
            return await self._handle_transfer_init(request)

        @app.get("/api/v1/transfers/uploads/{transfer_id}")
        async def upload_status(transfer_id: str, request: Request):
            return await self._handle_transfer_status(request, transfer_id)

        @app.put("/api/v1/transfers/uploads/{transfer_id}/chunks/{chunk_index}")
        async def upload_chunk(transfer_id: str, chunk_index: int, request: Request):
            return await self._handle_transfer_chunk(request, transfer_id, chunk_index)

        @app.post("/api/v1/transfers/uploads/{transfer_id}/cancel")
        async def upload_cancel(transfer_id: str, request: Request):
            return await self._handle_transfer_cancel(request, transfer_id)

        @app.post("/api/v1/transfers/downloads/init")
        async def download_init(request: Request):
            return await self._handle_download_init(request)

        @app.get("/api/v1/transfers/downloads/{download_id}")
        async def download_status(download_id: str, request: Request):
            return await self._handle_download_status(request, download_id)

        @app.get("/api/v1/transfers/downloads/{download_id}/chunks/{chunk_index}")
        async def download_chunk(download_id: str, chunk_index: int, request: Request):
            return await self._handle_download_chunk(request, download_id, chunk_index)

        @app.post("/api/v1/transfers/downloads/{download_id}/complete")
        async def download_complete(download_id: str, request: Request):
            return await self._handle_download_complete(request, download_id)

        @app.post("/api/v1/transfers/downloads/{download_id}/cancel")
        async def download_cancel(download_id: str, request: Request):
            return await self._handle_download_cancel(request, download_id)

        @app.get("/api/v1/ping")
        async def ping(authorization: str = Header(None)):
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Missing bearer token",
                )
            token = authorization.split("Bearer ", 1)[1].strip()
            session = self._pairing_manager.active_session
            if not session or session.bearer_token != token or session.is_expired():
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired session token",
                )
            return {"ok": True, "pong": True}

        @app.delete("/api/v1/session")
        async def end_session(authorization: str = Header(None)):
            if authorization and authorization.startswith("Bearer "):
                token = authorization.split("Bearer ", 1)[1].strip()
                session = self._pairing_manager.active_session
                if session and hmac.compare_digest(str(session.bearer_token), str(token)):
                    session_id = session.session_id
                    self._pairing_manager.revoke_device(session_id)
                    if self._transfer_manager is not None:
                        self._transfer_manager.cancel_session(session_id, "session_revoked")
                    if self._download_manager is not None:
                        self._download_manager.cancel_session(session_id, "session_revoked")
            return {"ok": True}

    # ── Transfer routes ────────────────────────────────────────────────────

    def _unsigned_transfer_error(self, exc: TransferError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "ok": False,
                "error_code": exc.code,
                "error_message": exc.message,
            },
        )

    def _signed_transfer_response(
        self,
        auth: TransferAuth,
        *,
        response_context: str,
        http_status: int,
        state: str,
        transfer_id: str,
        payload: dict[str, Any],
    ) -> JSONResponse:
        payload_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        payload_sha256 = hashlib.sha256(payload_bytes).digest()
        canonical = build_canonical_transfer_response(
            version=TRANSFER_PROTOCOL_VERSION,
            response_context=response_context,
            session_id=auth.session.session_id,
            http_status=http_status,
            sequence=auth.sequence,
            nonce=auth.nonce,
            state=state or "error",
            transfer_id=transfer_id,
            payload_length=len(payload_bytes),
            payload_sha256=payload_sha256,
        )
        signature = compute_transfer_response_signature(
            auth.session.downstream_key, canonical
        )
        return JSONResponse(
            status_code=http_status,
            content={
                "ok": 200 <= http_status < 300,
                "v": TRANSFER_PROTOCOL_VERSION,
                "ctx": response_context,
                "sid": auth.session.session_id,
                "seq": auth.sequence,
                "nonce_b64": encode_nonce_b64(auth.nonce),
                "http_status": http_status,
                "state": state or "error",
                "tid": transfer_id,
                "payload_b64": base64.b64encode(payload_bytes).decode("ascii"),
                "payload_sha256": payload_sha256.hex(),
                "sig": signature.hex(),
            },
        )

    def _signed_transfer_error(
        self,
        auth: TransferAuth,
        response_context: str,
        exc: TransferError,
        *,
        transfer_id: str = "",
    ) -> JSONResponse:
        transfer = None
        if self._transfer_manager is not None and transfer_id:
            try:
                transfer = self._transfer_manager.get_transfer(
                    transfer_id, session_id=auth.session.session_id
                )
            except TransferError:
                transfer = None
        if transfer is not None:
            payload: dict[str, Any] = public_transfer(transfer)
            state = transfer.state.value
            signed_transfer_id = transfer.transfer_id
        else:
            payload = {"transfer_id": transfer_id} if transfer_id else {}
            state = "error"
            signed_transfer_id = transfer_id
        payload["error_code"] = exc.code
        payload["error_message"] = exc.message
        payload.update(exc.details)
        return self._signed_transfer_response(
            auth,
            response_context=response_context,
            http_status=exc.http_status,
            state=state,
            transfer_id=signed_transfer_id,
            payload=payload,
        )

    def _signed_download_error(
        self,
        auth: TransferAuth,
        response_context: str,
        exc: DownloadError | TransferError,
        *,
        download_id: str = "",
    ) -> JSONResponse:
        ticket = None
        if self._download_manager is not None and download_id:
            try:
                # Scope metadata lookup to the authenticated device so an
                # unrelated session can never learn another ticket's filename,
                # size, digest, or progress from a signed error payload.
                ticket = self._download_manager.get(auth.session, download_id)
            except DownloadError:
                ticket = None
        if ticket is not None:
            payload: dict[str, Any] = public_download(ticket)
            state = ticket.state.value
            signed_download_id = ticket.download_id
        else:
            payload = {"download_id": download_id} if download_id else {}
            state = "error"
            signed_download_id = download_id
        payload["error_code"] = exc.code
        payload["error_message"] = exc.message
        payload.update(exc.details)
        return self._signed_transfer_response(
            auth,
            response_context=response_context,
            http_status=exc.http_status,
            state=state,
            transfer_id=signed_download_id,
            payload=payload,
        )

    def _require_download_manager(self) -> DownloadManager:
        if self._download_manager is None:
            raise DownloadError(
                "download_unavailable", "File downloads are not enabled.", http_status=503
            )
        return self._download_manager

    def _authenticate_transfer_request(
        self, request: Request, *, require_content_length: bool
    ) -> TransferAuth:
        if self._transfer_manager is None:
            raise TransferError(
                "transfer_unavailable", "File transfer is not enabled.", http_status=503
            )

        headers = request.headers
        authorization = headers.get("authorization")
        bearer_token = None
        if authorization and authorization.startswith("Bearer "):
            bearer_token = authorization[len("Bearer ") :].strip()

        content_length_header = headers.get("content-length")
        if content_length_header is None:
            if require_content_length:
                raise TransferError(
                    "invalid_content_length",
                    "Content-Length is required for this transfer request.",
                    http_status=400,
                )
            content_length = 0
        else:
            try:
                content_length = int(content_length_header)
            except (TypeError, ValueError) as exc:
                raise TransferError(
                    "invalid_content_length", "Content-Length is invalid.", http_status=400
                ) from exc
            if content_length < 0:
                raise TransferError(
                    "invalid_content_length", "Content-Length is invalid.", http_status=400
                )

        transfer_encoding = headers.get("transfer-encoding")
        if transfer_encoding and transfer_encoding.strip().lower() != "identity":
            raise TransferError(
                "invalid_content_length",
                "Chunked transfer encoding is not accepted; Content-Length is required.",
                http_status=400,
            )

        signature_hex = headers.get("x-kivo-signature")
        signature = _hex_to_bytes(signature_hex, 32)
        if signature is None:
            raise TransferError(
                "invalid_signature", "The request signature header is malformed.", http_status=401
            )

        try:
            auth = self._transfer_manager.authenticate_request(
                bearer_token=bearer_token,
                protocol_version=headers.get("x-kivo-protocol"),
                session_id=headers.get("x-kivo-session"),
                method=request.method,
                endpoint=request.url.path,
                sequence_value=headers.get("x-kivo-sequence"),
                nonce_b64=headers.get("x-kivo-nonce"),
                timestamp_value=headers.get("x-kivo-timestamp"),
                content_length=content_length,
                content_sha256_hex=headers.get("x-kivo-content-sha256"),
            )
        except TransferError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak internal details
            raise TransferError(
                "invalid_request", "The transfer request could not be authenticated.", http_status=400
            ) from exc

        if not self._transfer_manager.verify_request_signature(
            auth, auth.canonical_request, signature
        ):
            raise TransferError(
                "invalid_signature", "The request MAC could not be verified.", http_status=401
            )
        return auth

    async def _read_transfer_body(
        self, request: Request, auth: TransferAuth, *, limit: int
    ) -> bytes:
        if auth.content_length > limit:
            raise TransferError(
                "body_too_large",
                "The request body exceeds the allowed size for this endpoint.",
                http_status=413,
            )

        async def _consume() -> bytes:
            total = 0
            parts: list[bytes] = []
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise TransferError(
                        "body_too_large",
                        "The request body exceeds the allowed size for this endpoint.",
                        http_status=413,
                    )
                parts.append(chunk)
            return b"".join(parts)

        try:
            body = await asyncio.wait_for(
                _consume(), timeout=TRANSFER_RECEIVE_TIMEOUT_SECONDS
            )
        except TransferError:
            raise
        except asyncio.TimeoutError as exc:
            raise TransferError(
                "receive_timeout",
                "The request body was not received within the bounded timeout.",
                http_status=408,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - malformed transport
            raise TransferError(
                "receive_failed", "The request body could not be received.", http_status=400
            ) from exc

        if len(body) != auth.content_length:
            raise TransferError(
                "content_length_mismatch",
                "The received body length does not match the authenticated Content-Length.",
                http_status=400,
            )
        actual_sha256 = hashlib.sha256(body).digest()
        if not hmac.compare_digest(actual_sha256, auth.content_sha256):
            raise TransferError(
                "body_hash_mismatch",
                "The received body does not match the authenticated SHA-256.",
                http_status=400,
            )
        return body

    @staticmethod
    def _parse_json_object(body: bytes) -> dict[str, Any]:
        if not body:
            raise TransferError("invalid_json", "A JSON request body is required.", http_status=400)
        if len(body) > MAX_CONTROL_BODY_BYTES:
            raise TransferError("body_too_large", "The JSON body is too large.", http_status=413)

        def _no_duplicates(pairs):
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_key")
                result[key] = value
            return result

        try:
            data = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise TransferError(
                "invalid_json", "The request body is not a valid JSON object.", http_status=400
            ) from exc
        if not isinstance(data, dict):
            raise TransferError("invalid_json", "The request body must be a JSON object.", http_status=400)
        return data

    async def _handle_transfer_init(self, request: Request) -> JSONResponse:
        try:
            auth = self._authenticate_transfer_request(request, require_content_length=True)
        except TransferError as exc:
            return self._unsigned_transfer_error(exc)

        try:
            body = await self._read_transfer_body(request, auth, limit=MAX_CONTROL_BODY_BYTES)
            self._transfer_manager.commit_request(auth)  # type: ignore[union-attr]
            data = self._parse_json_object(body)
            unknown = set(data) - {"filename", "size", "sha256", "mime"}
            if unknown:
                raise TransferError(
                    "invalid_params",
                    "unknown parameter(s): " + ", ".join(sorted(unknown)),
                    http_status=400,
                )
            if "filename" not in data or "size" not in data or "sha256" not in data:
                raise TransferError(
                    "invalid_params",
                    "filename, size, and sha256 are required.",
                    http_status=400,
                )
            transfer = self._transfer_manager.create_upload(  # type: ignore[union-attr]
                auth.session,
                filename=data.get("filename"),
                size=data.get("size"),
                sha256_hex=data.get("sha256"),
                mime=data.get("mime"),
            )
            return self._signed_transfer_response(
                auth,
                response_context="upload_init",
                http_status=202,
                state=transfer.state.value,
                transfer_id=transfer.transfer_id,
                payload=public_transfer(transfer),
            )
        except TransferError as exc:
            return self._signed_transfer_error(auth, "upload_init", exc)

    async def _handle_transfer_status(
        self, request: Request, transfer_id: str
    ) -> JSONResponse:
        try:
            auth = self._authenticate_transfer_request(request, require_content_length=False)
        except TransferError as exc:
            return self._unsigned_transfer_error(exc)

        try:
            await self._read_transfer_body(request, auth, limit=0)
            self._transfer_manager.commit_request(auth)  # type: ignore[union-attr]
            transfer = self._transfer_manager.get_transfer(  # type: ignore[union-attr]
                transfer_id, session_id=auth.session.session_id
            )
            return self._signed_transfer_response(
                auth,
                response_context="upload_status",
                http_status=200,
                state=transfer.state.value,
                transfer_id=transfer.transfer_id,
                payload=public_transfer(transfer),
            )
        except TransferError as exc:
            return self._signed_transfer_error(
                auth, "upload_status", exc, transfer_id=transfer_id
            )

    async def _handle_transfer_chunk(
        self, request: Request, transfer_id: str, chunk_index: int
    ) -> JSONResponse:
        try:
            auth = self._authenticate_transfer_request(request, require_content_length=True)
        except TransferError as exc:
            return self._unsigned_transfer_error(exc)

        try:
            if chunk_index < 0:
                raise TransferError(
                    "invalid_chunk_index", "The chunk index is invalid.", http_status=400
                )
            if auth.content_length > TRANSFER_CHUNK_BYTES:
                raise TransferError(
                    "chunk_too_large",
                    "The chunk exceeds the fixed 1 MiB transfer chunk size.",
                    http_status=413,
                )
            body = await self._read_transfer_body(
                request, auth, limit=TRANSFER_CHUNK_BYTES
            )
            self._transfer_manager.commit_request(auth)  # type: ignore[union-attr]
            transfer = self._transfer_manager.accept_chunk(  # type: ignore[union-attr]
                auth.session,
                transfer_id,
                chunk_index,
                body,
                auth.content_sha256,
            )
            return self._signed_transfer_response(
                auth,
                response_context="upload_chunk",
                http_status=200,
                state=transfer.state.value,
                transfer_id=transfer.transfer_id,
                payload=public_transfer(transfer),
            )
        except TransferError as exc:
            return self._signed_transfer_error(
                auth, "upload_chunk", exc, transfer_id=transfer_id
            )

    async def _handle_transfer_cancel(
        self, request: Request, transfer_id: str
    ) -> JSONResponse:
        try:
            auth = self._authenticate_transfer_request(request, require_content_length=False)
        except TransferError as exc:
            return self._unsigned_transfer_error(exc)

        try:
            await self._read_transfer_body(request, auth, limit=0)
            self._transfer_manager.commit_request(auth)  # type: ignore[union-attr]
            self._transfer_manager.get_transfer(  # type: ignore[union-attr]
                transfer_id, session_id=auth.session.session_id
            )
            transfer = self._transfer_manager.cancel(transfer_id)  # type: ignore[union-attr]
            return self._signed_transfer_response(
                auth,
                response_context="upload_cancel",
                http_status=200,
                state=transfer.state.value,
                transfer_id=transfer.transfer_id,
                payload=public_transfer(transfer),
            )
        except TransferError as exc:
            return self._signed_transfer_error(
                auth, "upload_cancel", exc, transfer_id=transfer_id
            )

    async def _handle_download_init(self, request: Request) -> JSONResponse:
        try:
            auth = self._authenticate_transfer_request(request, require_content_length=True)
        except TransferError as exc:
            return self._unsigned_transfer_error(exc)

        claimed_id = ""
        try:
            manager = self._require_download_manager()
            body = await self._read_transfer_body(request, auth, limit=MAX_DOWNLOAD_CONTROL_BODY_BYTES)
            self._transfer_manager.commit_request(auth)  # type: ignore[union-attr]
            data = self._parse_json_object(body)
            unknown = set(data) - {"ticket_id"}
            if unknown:
                raise DownloadError(
                    "invalid_params",
                    "unknown parameter(s): " + ", ".join(sorted(unknown)),
                    http_status=400,
                )
            ticket_id = data.get("ticket_id")
            if isinstance(ticket_id, str):
                claimed_id = ticket_id
            ticket = manager.claim(auth.session, ticket_id)
            return self._signed_transfer_response(
                auth,
                response_context="download_init",
                http_status=200,
                state=ticket.state.value,
                transfer_id=ticket.download_id,
                payload=public_download(ticket),
            )
        except DownloadError as exc:
            return self._signed_download_error(
                auth, "download_init", exc, download_id=claimed_id
            )
        except TransferError as exc:
            return self._signed_download_error(
                auth, "download_init", exc, download_id=claimed_id
            )

    async def _handle_download_status(
        self, request: Request, download_id: str
    ) -> JSONResponse:
        try:
            auth = self._authenticate_transfer_request(request, require_content_length=False)
        except TransferError as exc:
            return self._unsigned_transfer_error(exc)

        try:
            manager = self._require_download_manager()
            await self._read_transfer_body(request, auth, limit=0)
            self._transfer_manager.commit_request(auth)  # type: ignore[union-attr]
            ticket = manager.get(auth.session, download_id)
            return self._signed_transfer_response(
                auth,
                response_context="download_status",
                http_status=200,
                state=ticket.state.value,
                transfer_id=ticket.download_id,
                payload=public_download(ticket),
            )
        except (DownloadError, TransferError) as exc:
            return self._signed_download_error(
                auth, "download_status", exc, download_id=download_id
            )

    async def _handle_download_chunk(
        self, request: Request, download_id: str, chunk_index: int
    ) -> JSONResponse:
        try:
            auth = self._authenticate_transfer_request(request, require_content_length=False)
        except TransferError as exc:
            return self._unsigned_transfer_error(exc)

        try:
            manager = self._require_download_manager()
            await self._read_transfer_body(request, auth, limit=0)
            self._transfer_manager.commit_request(auth)  # type: ignore[union-attr]
            chunk = manager.read_chunk(auth.session, download_id, chunk_index)
            ticket = manager.get(auth.session, download_id)
            payload = public_download(ticket)
            payload.update(self._download_chunk_payload(chunk))
            return self._signed_transfer_response(
                auth,
                response_context="download_chunk",
                http_status=200,
                state=chunk.state,
                transfer_id=chunk.download_id,
                payload=payload,
            )
        except (DownloadError, TransferError) as exc:
            return self._signed_download_error(
                auth, "download_chunk", exc, download_id=download_id
            )

    async def _handle_download_complete(
        self, request: Request, download_id: str
    ) -> JSONResponse:
        try:
            auth = self._authenticate_transfer_request(request, require_content_length=False)
        except TransferError as exc:
            return self._unsigned_transfer_error(exc)

        try:
            manager = self._require_download_manager()
            await self._read_transfer_body(request, auth, limit=0)
            self._transfer_manager.commit_request(auth)  # type: ignore[union-attr]
            ticket = manager.complete(auth.session, download_id)
            return self._signed_transfer_response(
                auth,
                response_context="download_complete",
                http_status=200,
                state=ticket.state.value,
                transfer_id=ticket.download_id,
                payload=public_download(ticket),
            )
        except (DownloadError, TransferError) as exc:
            return self._signed_download_error(
                auth, "download_complete", exc, download_id=download_id
            )

    async def _handle_download_cancel(
        self, request: Request, download_id: str
    ) -> JSONResponse:
        try:
            auth = self._authenticate_transfer_request(request, require_content_length=False)
        except TransferError as exc:
            return self._unsigned_transfer_error(exc)

        try:
            manager = self._require_download_manager()
            await self._read_transfer_body(request, auth, limit=0)
            self._transfer_manager.commit_request(auth)  # type: ignore[union-attr]
            ticket = manager.cancel(auth.session, download_id)
            return self._signed_transfer_response(
                auth,
                response_context="download_cancel",
                http_status=200,
                state=ticket.state.value,
                transfer_id=ticket.download_id,
                payload=public_download(ticket),
            )
        except (DownloadError, TransferError) as exc:
            return self._signed_download_error(
                auth, "download_cancel", exc, download_id=download_id
            )

    @staticmethod
    def _download_chunk_payload(chunk: DownloadChunk) -> dict[str, Any]:
        return {
            "chunk_index": chunk.chunk_index,
            "offset": chunk.offset,
            "length": len(chunk.data),
            "chunk_sha256": chunk.sha256.hex(),
            "chunk_b64": base64.b64encode(chunk.data).decode("ascii"),
        }

    def start(self) -> bool:
        """Start ephemeral Uvicorn server in background thread with TLS 1.3."""
        if self._running:
            return True

        self._temp_dir = tempfile.TemporaryDirectory(prefix="kivo_bridge_ssl_")
        cert_file = os.path.join(self._temp_dir.name, "cert.pem")
        key_file = os.path.join(self._temp_dir.name, "key.pem")

        with open(cert_file, "wb") as f:
            f.write(self._cert_pem)
        os.chmod(cert_file, 0o600)

        with open(key_file, "wb") as f:
            f.write(self._key_pem)
        os.chmod(key_file, 0o600)

        config = uvicorn.Config(
            app=self._app,
            host=self._lan_ip,
            port=self._port,
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
            ssl_version=ssl.PROTOCOL_TLS_SERVER,
            log_level="warning",
            access_log=False,
        )

        server = uvicorn.Server(config)
        self._server = server
        self._running = True

        self._thread = threading.Thread(
            target=server.run,
            name="kivo-bridge-server",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> None:
        """Gracefully stop server and join thread."""
        self._running = False
        if self._transfer_manager is not None:
            try:
                self._transfer_manager.cancel_all("server_stopped")
            except Exception:
                pass
        if self._download_manager is not None:
            try:
                self._download_manager.cancel_all("server_stopped")
            except Exception:
                pass
        server = self._server
        if server is not None:
            server.should_exit = True
            self._server = None

        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and threading.current_thread() is not thread
        ):
            thread.join(timeout=max(0.1, float(timeout)))
            self._thread = None

        if self._temp_dir is not None:
            try:
                self._temp_dir.cleanup()
            except OSError:
                pass
            self._temp_dir = None


__all__ = ["BridgeServer"]
