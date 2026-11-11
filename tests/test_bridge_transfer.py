"""Transfer manager state machine, authentication, and lifecycle tests."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from kivo.bridge.models import PairedSession
from kivo.bridge.quarantine import QuarantineManager
from kivo.bridge.transfer import (
    TERMINAL_STATES,
    TransferError,
    TransferManager,
    UploadState,
    UploadTransfer,
    public_transfer,
)
from kivo.bridge.transfer_crypto import (
    TRANSFER_PROTOCOL_VERSION,
    build_canonical_transfer_request,
    compute_transfer_request_signature,
    encode_nonce_b64,
)
from kivo.bridge.transfer_validation import (
    MAX_CONCURRENT_TRANSFERS,
    TRANSFER_CHUNK_BYTES,
)


def _make_session(
    *,
    session_id: str = "sess_test123",
    client_id: str = "client_test",
    device_name: str = "Test Phone",
    k_up: bytes | None = None,
    k_down: bytes | None = None,
    bearer_token: str = "sess_tok_test_bearer",
    ttl: float = 7200.0,
) -> PairedSession:
    now = time.time()
    return PairedSession(
        session_id=session_id,
        client_id=client_id,
        device_name=device_name,
        client_ip="127.0.0.1",
        bearer_token=bearer_token,
        upstream_key=k_up or os.urandom(32),
        downstream_key=k_down or os.urandom(32),
        created_at=now,
        last_active=now,
        expires_at=now + ttl,
    )


def _make_manager(
    tmp: str,
    session: PairedSession | None = None,
    *,
    free_bytes: int = 20 * 1024**3,
    clock: float | None = None,
) -> tuple[TransferManager, PairedSession]:
    sess = session or _make_session()
    events: list[tuple[str, dict]] = []
    qm = QuarantineManager(
        runtime_base=os.path.join(tmp, "runtime"),
        cache_base=os.path.join(tmp, "cache"),
        downloads_dir=os.path.join(tmp, "Downloads"),
        free_space_fn=lambda root: free_bytes,
    )
    os.makedirs(os.path.join(tmp, "Downloads"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "runtime"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "cache"), exist_ok=True)
    clock_fn = (lambda: clock) if clock is not None else time.time
    tm = TransferManager(
        session_provider=lambda: sess,
        quarantine=qm,
        on_event=lambda e, d: events.append((e, d)),
        clock=clock_fn,
        cleanup_interval=999.0,  # disable automatic cleanup in tests
    )
    tm.start()
    tm._events = events  # type: ignore[attr-defined]
    return tm, sess


def _auth_headers(
    session: PairedSession,
    *,
    method: str,
    endpoint: str,
    sequence: int,
    content: bytes = b"",
    clock: float | None = None,
) -> dict[str, str]:
    """Build authenticated transfer request headers for testing."""
    nonce = os.urandom(16)
    ts = clock or time.time()
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


class CreateUploadTests(unittest.TestCase):

    def test_valid_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                data = b"file content"
                sha256 = hashlib.sha256(data).digest()
                transfer = tm.create_upload(
                    sess,
                    filename="test.txt",
                    size=len(data),
                    sha256_hex=sha256.hex(),
                    mime="text/plain",
                )
                self.assertEqual(transfer.state, UploadState.AWAITING_DESKTOP_APPROVAL)
                self.assertEqual(transfer.filename, "test.txt")
                self.assertEqual(transfer.size, len(data))
                self.assertTrue(transfer.transfer_id.startswith("tr_"))
                self.assertEqual(len(transfer.transfer_id), 27)
            finally:
                tm.close()

    def test_create_rejects_invalid_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                with self.assertRaises(TransferError) as ctx:
                    tm.create_upload(
                        sess, filename="../malicious.sh",
                        size=100, sha256_hex="a" * 64,
                    )
                self.assertEqual(ctx.exception.code, "invalid_filename")
            finally:
                tm.close()

    def test_create_rejects_oversized(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                sha256 = hashlib.sha256(b"").digest()
                with self.assertRaises(TransferError):
                    tm.create_upload(
                        sess, filename="big.bin",
                        size=3 * 1024**3, sha256_hex=sha256.hex(),
                    )
            finally:
                tm.close()

    def test_create_rejects_expired_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            expired = _make_session(ttl=-1.0)
            tm, _ = _make_manager(tmp, session=expired)
            try:
                sha256 = hashlib.sha256(b"").digest()
                with self.assertRaises(TransferError) as ctx:
                    tm.create_upload(
                        expired, filename="file.txt",
                        size=10, sha256_hex=sha256.hex(),
                    )
                self.assertEqual(ctx.exception.http_status, 401)
            finally:
                tm.close()

    def test_create_emits_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                sha256 = hashlib.sha256(b"test").digest()
                tm.create_upload(
                    sess, filename="test.txt",
                    size=4, sha256_hex=sha256.hex(),
                )
                events = tm._events  # type: ignore[attr-defined]
                self.assertTrue(any(e[0] == "bridge_transfer_requested" for e in events))
            finally:
                tm.close()


class TwoGateApprovalTests(unittest.TestCase):

    def test_reject_receive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                sha256 = hashlib.sha256(b"test").digest()
                transfer = tm.create_upload(
                    sess, filename="test.txt", size=4, sha256_hex=sha256.hex(),
                )
                result = tm.resolve(transfer.transfer_id, "reject_receive")
                self.assertEqual(result.state, UploadState.REJECTED)
                self.assertEqual(tm.list_public()[0]["state"], "rejected")
            finally:
                tm.close()

    def test_allow_then_reject_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                data = b"test"
                sha256 = hashlib.sha256(data).digest()
                transfer = tm.create_upload(
                    sess, filename="test.txt", size=len(data), sha256_hex=sha256.hex(),
                )
                tm.resolve(transfer.transfer_id, "allow_receive")
                with self.assertRaises(TransferError) as ctx:
                    tm.resolve(transfer.transfer_id, "reject_receive")
                self.assertEqual(ctx.exception.http_status, 409)
            finally:
                tm.close()

    def test_full_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                data = b"hello world transfer"
                size = len(data)
                sha256 = hashlib.sha256(data).digest()

                transfer = tm.create_upload(
                    sess, filename="hello.txt", size=size, sha256_hex=sha256.hex(),
                )
                self.assertEqual(transfer.state, UploadState.AWAITING_DESKTOP_APPROVAL)

                tm.resolve(transfer.transfer_id, "allow_receive")
                transfer = tm.get_transfer(transfer.transfer_id)
                if size == 0:
                    self.assertIn(
                        transfer.state,
                        (UploadState.AWAITING_FINAL_ACCEPTANCE, UploadState.VERIFYING),
                    )
                else:
                    self.assertEqual(transfer.state, UploadState.RECEIVING)

                    chunk_sha256 = hashlib.sha256(data).digest()
                    transfer = tm.accept_chunk(
                        sess, transfer.transfer_id, 0, data, chunk_sha256,
                    )
                    self.assertIn(
                        transfer.state,
                        (UploadState.AWAITING_FINAL_ACCEPTANCE, UploadState.VERIFYING),
                    )

                transfer = tm.get_transfer(transfer.transfer_id)
                self.assertEqual(transfer.state, UploadState.AWAITING_FINAL_ACCEPTANCE)

                result = tm.resolve(transfer.transfer_id, "accept_file")
                self.assertEqual(result.state, UploadState.COMMITTED)
                self.assertIsNotNone(result.published_filename)

                final = Path(tmp) / "Downloads" / result.published_filename
                self.assertTrue(final.exists())
                self.assertEqual(final.read_bytes(), data)
            finally:
                tm.close()

    def test_discard_verified_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                data = b"discard me"
                sha256 = hashlib.sha256(data).digest()
                transfer = tm.create_upload(
                    sess, filename="discard.txt", size=len(data), sha256_hex=sha256.hex(),
                )
                tm.resolve(transfer.transfer_id, "allow_receive")
                tm.accept_chunk(
                    sess, transfer.transfer_id, 0, data, hashlib.sha256(data).digest(),
                )
                transfer = tm.get_transfer(transfer.transfer_id)
                self.assertEqual(transfer.state, UploadState.AWAITING_FINAL_ACCEPTANCE)

                result = tm.resolve(transfer.transfer_id, "discard_file")
                self.assertEqual(result.state, UploadState.DISCARDED)

                downloads = Path(tmp) / "Downloads"
                self.assertEqual(list(downloads.iterdir()), [])
            finally:
                tm.close()


class ChunkTests(unittest.TestCase):

    def test_wrong_transfer_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                with self.assertRaises(TransferError) as ctx:
                    tm.accept_chunk(
                        sess, "tr_000000000000000000000000", 0, b"x", hashlib.sha256(b"x").digest(),
                    )
                self.assertEqual(ctx.exception.http_status, 404)
            finally:
                tm.close()

    def test_chunk_before_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                data = b"test data chunk"
                sha256 = hashlib.sha256(data).digest()
                transfer = tm.create_upload(
                    sess, filename="f.txt", size=len(data), sha256_hex=sha256.hex(),
                )
                with self.assertRaises(TransferError) as ctx:
                    tm.accept_chunk(
                        sess, transfer.transfer_id, 0, data, hashlib.sha256(data).digest(),
                    )
                self.assertEqual(ctx.exception.http_status, 409)
            finally:
                tm.close()

    def test_out_of_order_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                data = b"\x00" * (TRANSFER_CHUNK_BYTES * 2)
                sha256 = hashlib.sha256(data).digest()
                transfer = tm.create_upload(
                    sess, filename="big.bin", size=len(data), sha256_hex=sha256.hex(),
                )
                tm.resolve(transfer.transfer_id, "allow_receive")

                with self.assertRaises(TransferError) as ctx:
                    tm.accept_chunk(
                        sess, transfer.transfer_id, 1,
                        data[TRANSFER_CHUNK_BYTES:],
                        hashlib.sha256(data[TRANSFER_CHUNK_BYTES:]).digest(),
                    )
                self.assertEqual(ctx.exception.code, "chunk_out_of_order")
            finally:
                tm.close()

    def test_wrong_chunk_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                data = b"chunk data"
                sha256 = hashlib.sha256(data).digest()
                transfer = tm.create_upload(
                    sess, filename="f.txt", size=len(data), sha256_hex=sha256.hex(),
                )
                tm.resolve(transfer.transfer_id, "allow_receive")

                with self.assertRaises(TransferError) as ctx:
                    tm.accept_chunk(
                        sess, transfer.transfer_id, 0, data, b"\xFF" * 32,
                    )
                self.assertEqual(ctx.exception.code, "chunk_hash_mismatch")
            finally:
                tm.close()

    def test_wrong_chunk_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                total = 100
                sha256 = hashlib.sha256(b"\x00" * total).digest()
                transfer = tm.create_upload(
                    sess, filename="f.bin", size=total, sha256_hex=sha256.hex(),
                )
                tm.resolve(transfer.transfer_id, "allow_receive")

                wrong_data = b"\x00" * 50
                with self.assertRaises(TransferError):
                    tm.accept_chunk(
                        sess, transfer.transfer_id, 0, wrong_data,
                        hashlib.sha256(wrong_data).digest(),
                    )
            finally:
                tm.close()


class EmptyFileTests(unittest.TestCase):

    def test_empty_file_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                sha256 = hashlib.sha256(b"").digest()
                transfer = tm.create_upload(
                    sess, filename="empty.txt", size=0, sha256_hex=sha256.hex(),
                )
                tm.resolve(transfer.transfer_id, "allow_receive")
                transfer = tm.get_transfer(transfer.transfer_id)
                self.assertEqual(transfer.state, UploadState.AWAITING_FINAL_ACCEPTANCE)
                result = tm.resolve(transfer.transfer_id, "accept_file")
                self.assertEqual(result.state, UploadState.COMMITTED)
                final = Path(tmp) / "Downloads" / result.published_filename
                self.assertTrue(final.exists())
                self.assertEqual(final.stat().st_size, 0)
            finally:
                tm.close()


class ConcurrencyLimitTests(unittest.TestCase):

    def test_concurrent_transfer_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess1 = _make_session(session_id="sess_1")
            sess2 = _make_session(session_id="sess_2")
            sess3 = _make_session(session_id="sess_3")

            current_session = sess1
            events: list[tuple[str, dict]] = []
            qm = QuarantineManager(
                runtime_base=os.path.join(tmp, "runtime"),
                cache_base=os.path.join(tmp, "cache"),
                downloads_dir=os.path.join(tmp, "Downloads"),
                free_space_fn=lambda root: 20 * 1024**3,
            )
            os.makedirs(os.path.join(tmp, "Downloads"), exist_ok=True)
            os.makedirs(os.path.join(tmp, "runtime"), exist_ok=True)
            os.makedirs(os.path.join(tmp, "cache"), exist_ok=True)
            tm = TransferManager(
                session_provider=lambda: current_session,
                quarantine=qm,
                on_event=lambda e, d: events.append((e, d)),
                cleanup_interval=999.0,
            )
            tm.start()
            try:
                # Session 1 starts transfer 1
                current_session = sess1
                d1 = b"file_1"
                t1 = tm.create_upload(
                    sess1, filename="f1.txt", size=len(d1), sha256_hex=hashlib.sha256(d1).hexdigest(),
                )
                tm.resolve(t1.transfer_id, "allow_receive")

                # Session 2 starts transfer 2 (max concurrent receiving is 2)
                current_session = sess2
                d2 = b"file_2"
                t2 = tm.create_upload(
                    sess2, filename="f2.txt", size=len(d2), sha256_hex=hashlib.sha256(d2).hexdigest(),
                )
                tm.resolve(t2.transfer_id, "allow_receive")

                # Session 3 creates transfer 3, but allow_receive is rejected due to concurrent receiving limit
                current_session = sess3
                d3 = b"file_3"
                t3 = tm.create_upload(
                    sess3, filename="f3.txt", size=len(d3), sha256_hex=hashlib.sha256(d3).hexdigest(),
                )
                with self.assertRaises(TransferError) as ctx:
                    tm.resolve(t3.transfer_id, "allow_receive")
                self.assertEqual(ctx.exception.code, "concurrent_transfer_limit")
                self.assertEqual(ctx.exception.http_status, 429)
            finally:
                tm.close()


class CancelTests(unittest.TestCase):

    def test_cancel_receiving(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                data = b"cancel me"
                sha256 = hashlib.sha256(data).digest()
                transfer = tm.create_upload(
                    sess, filename="c.txt", size=len(data), sha256_hex=sha256.hex(),
                )
                tm.resolve(transfer.transfer_id, "allow_receive")
                result = tm.cancel(transfer.transfer_id)
                self.assertEqual(result.state, UploadState.CANCELLED)
            finally:
                tm.close()

    def test_cancel_awaiting_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                sha256 = hashlib.sha256(b"x").digest()
                transfer = tm.create_upload(
                    sess, filename="c.txt", size=1, sha256_hex=sha256.hex(),
                )
                result = tm.cancel(transfer.transfer_id)
                self.assertEqual(result.state, UploadState.CANCELLED)
            finally:
                tm.close()

    def test_cancel_terminal_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                sha256 = hashlib.sha256(b"x").digest()
                transfer = tm.create_upload(
                    sess, filename="c.txt", size=1, sha256_hex=sha256.hex(),
                )
                tm.resolve(transfer.transfer_id, "reject_receive")
                with self.assertRaises(TransferError) as ctx:
                    tm.cancel(transfer.transfer_id)
                self.assertEqual(ctx.exception.http_status, 409)
            finally:
                tm.close()


class SessionRevocationTests(unittest.TestCase):

    def test_revoke_cancels_transfers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                sha256 = hashlib.sha256(b"x").digest()
                t1 = tm.create_upload(
                    sess, filename="a.txt", size=1, sha256_hex=sha256.hex(),
                )
                t2 = tm.create_upload(
                    sess, filename="b.txt", size=1, sha256_hex=sha256.hex(),
                )
                cancelled = tm.cancel_session(sess.session_id)
                self.assertEqual(cancelled, 2)
                for tid in (t1.transfer_id, t2.transfer_id):
                    transfer = tm.get_transfer(tid)
                    self.assertEqual(transfer.state, UploadState.CANCELLED)
            finally:
                tm.close()


class PublicTransferTests(unittest.TestCase):

    def test_no_secrets_in_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            try:
                sha256 = hashlib.sha256(b"x").digest()
                transfer = tm.create_upload(
                    sess, filename="test.txt", size=1, sha256_hex=sha256.hex(),
                )
                pub = public_transfer(transfer)
                for forbidden in (
                    "bearer_token",
                    "upstream_key",
                    "downstream_key",
                    "session_token",
                    "quarantine",
                    "reservation",
                    "incremental_hasher",
                ):
                    self.assertNotIn(forbidden, pub)
                for required in (
                    "transfer_id",
                    "filename",
                    "size",
                    "state",
                    "device_name",
                    "progress_percent",
                ):
                    self.assertIn(required, pub)
            finally:
                tm.close()


class LifecycleTests(unittest.TestCase):

    def test_stop_cancels_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            sha256 = hashlib.sha256(b"x").digest()
            tm.create_upload(sess, filename="a.txt", size=1, sha256_hex=sha256.hex())
            tm.stop()
            self.assertFalse(tm.is_running)

    def test_close_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            tm, sess = _make_manager(tmp)
            sha256 = hashlib.sha256(b"x").digest()
            tm.create_upload(sess, filename="a.txt", size=1, sha256_hex=sha256.hex())
            tm.close()


if __name__ == "__main__":
    unittest.main()
