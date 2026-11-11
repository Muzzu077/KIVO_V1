"""Download ticket security, source stability, and transfer edge-case tests."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kivo.bridge.download import (
    DownloadError,
    DownloadManager,
    DownloadState,
    public_download,
)
from kivo.bridge.download_validation import (
    DOWNLOAD_ACTIVE_LEASE_SECONDS,
    DOWNLOAD_CHUNK_BYTES,
    DOWNLOAD_TICKET_TTL_SECONDS,
    MAX_DOWNLOAD_BYTES,
)
from kivo.bridge.models import PairedSession
from kivo.shelf.path_policy import PathAuthorizer, ShelfPolicyError
from kivo.shelf.service import FileShelfService


def _make_session(
    *,
    session_id: str = "sess_download_test",
    client_id: str = "client_download_test",
    device_name: str = "Pixel Download",
    ttl: float = 100000.0,
) -> PairedSession:
    now = time.time()
    return PairedSession(
        session_id=session_id,
        client_id=client_id,
        device_name=device_name,
        client_ip="127.0.0.1",
        bearer_token="sess_tok_download_test",
        upstream_key=os.urandom(32),
        downstream_key=os.urandom(32),
        created_at=now,
        last_active=now,
        expires_at=now + ttl,
    )


class _Harness:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.authorizer = PathAuthorizer()
        self.shelf = FileShelfService(
            authorizer=self.authorizer,
            xdg_open_path="/bin/true",
        )
        self.shelf.start()
        self.session: PairedSession | None = _make_session()
        self.now = time.time()
        self.events: list[tuple[str, dict]] = []
        self.manager = DownloadManager(
            session_provider=lambda: self.session,
            on_event=lambda event, payload: self.events.append((event, payload)),
            clock=lambda: self.now,
            cleanup_interval=999.0,
        )
        self.manager.start()

    def close(self) -> None:
        self.manager.close()
        self.shelf.close()

    def stage(self, name: str, data: bytes) -> str:
        path = self.root / name
        path.write_bytes(data)
        added, rejected = self.shelf.add_items([path.as_uri()])
        assert len(added) == 1, rejected
        return added[0].id

    def issue(self, item_id: str):
        grant = self.shelf.authorize_export(item_id)
        return self.manager.create_ticket(grant)


class DownloadTicketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.h = _Harness(Path(self.tmp.name).resolve())
        self.data = b"download payload " * 1000
        self.item_id = self.h.stage("report.bin", self.data)

    def tearDown(self) -> None:
        self.h.close()
        self.tmp.cleanup()

    def test_ticket_ids_are_256_bit_opaque_capabilities(self):
        first = self.h.issue(self.item_id)
        second = self.h.issue(self.item_id)
        self.assertRegex(first.download_id, r"^tkt_[0-9a-f]{64}$")
        self.assertEqual(len(first.download_id), 4 + 64)
        self.assertNotEqual(first.download_id, second.download_id)
        self.assertTrue(first.download_id != self.item_id)

    def test_ticket_is_bound_to_session_device_and_file(self):
        ticket = self.h.issue(self.item_id)
        self.assertEqual(ticket.session_id, self.h.session.session_id)
        self.assertEqual(ticket.device_id, self.h.session.client_id)
        self.assertEqual(ticket.size, len(self.data))
        self.assertEqual(ticket.sha256, hashlib.sha256(self.data).digest())
        self.assertEqual(ticket.filename, "report.bin")
        self.assertEqual(ticket.state, DownloadState.ISSUED)

    def test_staging_alone_is_not_authorization(self):
        with self.assertRaises(ShelfPolicyError) as ctx:
            self.h.shelf.authorize_export("shelf_000000000000")
        self.assertEqual(ctx.exception.code, "item_not_found")
        with self.assertRaises(ShelfPolicyError) as ctx:
            self.h.shelf.authorize_export("../../etc/passwd")
        self.assertEqual(ctx.exception.code, "item_not_found")

    def test_claim_is_idempotent_for_bound_session(self):
        ticket = self.h.issue(self.item_id)
        first = self.h.manager.claim(self.h.session, ticket.download_id)
        second = self.h.manager.claim(self.h.session, ticket.download_id)
        self.assertEqual(first.state, DownloadState.CLAIMED)
        self.assertEqual(second.state, DownloadState.CLAIMED)
        self.assertEqual(first.claimed_at, second.claimed_at)
        claims = [
            event for event, payload in self.h.events
            if event == "bridge_download_progress"
            and payload.get("state") == "claimed"
        ]
        self.assertEqual(len(claims), 1)

    def test_other_session_or_device_cannot_claim(self):
        ticket = self.h.issue(self.item_id)
        other_session = _make_session(
            session_id="sess_other", client_id="client_other"
        )
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.claim(other_session, ticket.download_id)
        self.assertEqual(ctx.exception.code, "download_forbidden")
        same_session_new_device = _make_session(
            session_id=self.h.session.session_id, client_id="client_imposter"
        )
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.claim(same_session_new_device, ticket.download_id)
        self.assertEqual(ctx.exception.code, "download_forbidden")

    def test_ticket_expires_after_five_minutes_and_releases_descriptor(self):
        ticket = self.h.issue(self.item_id)
        self.h.now += DOWNLOAD_TICKET_TTL_SECONDS + 1
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.claim(self.h.session, ticket.download_id)
        self.assertEqual(ctx.exception.code, "download_expired")
        self.assertEqual(ticket.state, DownloadState.EXPIRED)
        self.assertIsNone(ticket.grant)

    def test_claim_binds_a_finite_active_lease_without_extending_it(self):
        ticket = self.h.issue(self.item_id)
        claimed = self.h.manager.claim(self.h.session, ticket.download_id)
        first_deadline = claimed.active_expires_at
        self.assertAlmostEqual(
            first_deadline,
            self.h.now + DOWNLOAD_ACTIVE_LEASE_SECONDS,
            places=3,
        )
        self.assertEqual(claimed.expires_at, ticket.expires_at)

        self.h.now += 120.0
        again = self.h.manager.claim(self.h.session, ticket.download_id)
        self.assertEqual(again.active_expires_at, first_deadline)
        self.assertEqual(again.claimed_at, claimed.claimed_at)

    def test_claimed_transfer_survives_ticket_ttl_but_not_the_lease(self):
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        self.h.now += DOWNLOAD_TICKET_TTL_SECONDS + 1
        ticket.last_activity = self.h.now

        chunk = self.h.manager.read_chunk(self.h.session, ticket.download_id, 0)
        self.assertEqual(chunk.bytes_sent, len(chunk.data))
        self.assertEqual(
            self.h.manager.get(self.h.session, ticket.download_id).state,
            DownloadState.STREAMING,
        )

        self.h.now += DOWNLOAD_ACTIVE_LEASE_SECONDS
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.read_chunk(self.h.session, ticket.download_id, 1)
        self.assertEqual(ctx.exception.code, "download_expired")
        self.assertEqual(ticket.state, DownloadState.EXPIRED)
        self.assertEqual(ticket.error_code, "active_lease_expired")
        self.assertIsNone(ticket.grant)

    def test_cleanup_and_status_use_the_effective_deadline(self):
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        self.h.now += DOWNLOAD_TICKET_TTL_SECONDS + 1
        ticket.last_activity = self.h.now
        self.h.manager.cleanup_expired()
        self.assertEqual(ticket.state, DownloadState.CLAIMED)
        self.h.now += DOWNLOAD_ACTIVE_LEASE_SECONDS
        self.h.manager.cleanup_expired()
        self.assertEqual(ticket.state, DownloadState.EXPIRED)
        self.assertEqual(ticket.error_code, "active_lease_expired")

    def test_public_payload_exposes_lease_and_device_binding(self):
        ticket = self.h.issue(self.item_id)
        payload = public_download(ticket)
        self.assertEqual(payload["device_id"], self.h.session.client_id)
        self.assertEqual(payload["claim_expires_at"], round(ticket.expires_at, 3))
        self.assertEqual(payload["active_expires_at"], 0.0)
        self.assertEqual(payload["expires_at"], round(ticket.expires_at, 3))

        self.h.manager.claim(self.h.session, ticket.download_id)
        claimed = public_download(ticket)
        self.assertEqual(
            claimed["expires_at"], round(ticket.active_expires_at, 3)
        )
        self.assertEqual(claimed["claim_expires_at"], round(ticket.expires_at, 3))

    def test_cleanup_expires_tickets_and_releases_descriptors(self):
        ticket = self.h.issue(self.item_id)
        self.h.now += DOWNLOAD_TICKET_TTL_SECONDS + 1
        self.h.manager.cleanup_expired()
        self.assertEqual(ticket.state, DownloadState.EXPIRED)
        self.assertIsNone(ticket.grant)

    def test_session_revocation_revokes_ticket(self):
        ticket = self.h.issue(self.item_id)
        self.h.session = None
        self.assertEqual(self.h.manager.cancel_session(ticket.session_id), 1)
        self.assertEqual(ticket.state, DownloadState.REVOKED)
        self.assertIsNone(ticket.grant)
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.claim(
                _make_session(session_id=ticket.session_id, client_id=ticket.device_id),
                ticket.download_id,
            )
        self.assertEqual(ctx.exception.code, "session_expired")

    def test_create_ticket_requires_active_session_and_closes_grant(self):
        grant = self.h.shelf.authorize_export(self.item_id)
        self.h.session = None
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.create_ticket(grant)
        self.assertEqual(ctx.exception.code, "no_active_session")
        self.assertTrue(grant.closed)

    def test_desktop_cancel_is_idempotent(self):
        ticket = self.h.issue(self.item_id)
        first = self.h.manager.cancel_desktop(ticket.download_id)
        second = self.h.manager.cancel_desktop(ticket.download_id)
        self.assertEqual(first.state, DownloadState.CANCELLED)
        self.assertEqual(second.state, DownloadState.CANCELLED)
        self.assertIsNone(ticket.grant)

    def test_public_payload_never_leaks_paths_or_identity(self):
        ticket = self.h.issue(self.item_id)
        payload = public_download(ticket)
        encoded = json.dumps(payload)
        self.assertNotIn(str(self.h.root), encoded)
        self.assertNotIn(self.item_id, encoded)
        for forbidden in (
            "canonical_path", "item_id", "fd", "inode", "ino", "dev",
            "mnt_id", "identity", "private",
        ):
            self.assertNotIn(forbidden, payload)

    def test_events_are_path_free_and_reflect_state(self):
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        chunk = self.h.manager.read_chunk(self.h.session, ticket.download_id, 0)
        self.h.manager.complete(self.h.session, ticket.download_id)
        names = [event for event, _ in self.h.events]
        self.assertIn("bridge_download_requested", names)
        self.assertIn("bridge_download_progress", names)
        self.assertIn("bridge_download_completed", names)
        for _, payload in self.h.events:
            self.assertNotIn(str(self.h.root), json.dumps(payload))
        completed = [
            payload for event, payload in self.h.events
            if event == "bridge_download_completed"
        ][-1]
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["bytes_sent"], len(self.data))
        self.assertEqual(len(chunk.data), len(self.data))


class DownloadSourceStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.h = _Harness(Path(self.tmp.name).resolve())
        self.data = os.urandom(DOWNLOAD_CHUNK_BYTES + 1234)
        self.item_id = self.h.stage("source.bin", self.data)
        self.path = self.h.root / "source.bin"

    def tearDown(self) -> None:
        self.h.close()
        self.tmp.cleanup()

    def test_in_place_modification_aborts_download(self):
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        with open(self.path, "r+b") as handle:
            handle.seek(10)
            handle.write(b"X" * 64)
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(
            self.path,
            ns=(ticket.identity.mtime_ns + 2_000_000, ticket.identity.mtime_ns + 2_000_000),
        )
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.read_chunk(self.h.session, ticket.download_id, 0)
        self.assertEqual(ctx.exception.code, "download_source_changed")
        self.assertEqual(ticket.state, DownloadState.FAILED)
        self.assertIsNone(ticket.grant)

    def test_path_replacement_cannot_substitute_streamed_bytes(self):
        ticket = self.h.issue(self.item_id)
        replacement = b"attacker controlled" * 100
        other = self.h.root / "replacement.bin"
        other.write_bytes(replacement)
        os.replace(other, self.path)
        self.h.manager.claim(self.h.session, ticket.download_id)
        received = b"".join(
            self.h.manager.read_chunk(self.h.session, ticket.download_id, index).data
            for index in range(ticket.total_chunks)
        )
        self.assertEqual(received, self.data)
        self.assertNotEqual(received, replacement)
        self.assertEqual(
            self.h.manager.complete(self.h.session, ticket.download_id).state,
            DownloadState.COMPLETED,
        )
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), hashlib.sha256(replacement).digest())

    def test_mount_identity_change_aborts_download(self):
        ticket = self.h.issue(self.item_id)
        if ticket.identity.mnt_id is None:
            self.skipTest("statx mount IDs are unavailable on this filesystem")
        ticket.identity = dataclasses.replace(
            ticket.identity, mnt_id=ticket.identity.mnt_id + 1
        )
        self.h.manager.claim(self.h.session, ticket.download_id)
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.read_chunk(self.h.session, ticket.download_id, 0)
        self.assertEqual(ctx.exception.code, "download_source_changed")
        self.assertEqual(ticket.state, DownloadState.FAILED)

    def test_symlink_replacement_is_rejected_at_export(self):
        self.path.unlink()
        sensitive = self.h.root / "id_rsa"
        sensitive.write_bytes(b"secret")
        self.path.symlink_to(sensitive)
        with self.assertRaises(ShelfPolicyError) as ctx:
            self.h.shelf.authorize_export(self.item_id)
        self.assertEqual(ctx.exception.code, "stale_reference")
        items = {item.id: item for item in self.h.shelf.list_items()}
        self.assertTrue(items[self.item_id].stale)

    def test_staging_rejects_symlink_and_sensitive_paths(self):
        target = self.h.root / "target.txt"
        target.write_bytes(b"target")
        link = self.h.root / "link.txt"
        link.symlink_to(target)
        added, rejected = self.h.shelf.add_items([link.as_uri()])
        self.assertEqual(added, [])
        self.assertEqual(rejected[0]["code"], "symlink_not_allowed")

        secret_dir = self.h.root / ".ssh"
        secret_dir.mkdir()
        secret = secret_dir / "id_ed25519"
        secret.write_bytes(b"private-key")
        added, rejected = self.h.shelf.add_items([secret.as_uri()])
        self.assertEqual(added, [])
        self.assertEqual(rejected[0]["code"], "sensitive_path_denied")

    def test_export_rejects_file_modified_after_staging(self):
        with open(self.path, "ab") as handle:
            handle.write(b"append")
        with self.assertRaises(ShelfPolicyError) as ctx:
            self.h.shelf.authorize_export(self.item_id)
        self.assertEqual(ctx.exception.code, "stale_reference")

    def test_source_is_never_modified(self):
        before = self.path.stat()
        before_hash = hashlib.sha256(self.path.read_bytes()).digest()
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        for index in range(ticket.total_chunks):
            self.h.manager.read_chunk(self.h.session, ticket.download_id, index)
        self.h.manager.complete(self.h.session, ticket.download_id)
        after = self.path.stat()
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mode, after.st_mode)
        self.assertEqual(before_hash, hashlib.sha256(self.path.read_bytes()).digest())

    def test_file_larger_than_two_gib_is_refused(self):
        sparse = self.h.root / "sparse.bin"
        with open(sparse, "wb") as handle:
            handle.truncate(MAX_DOWNLOAD_BYTES + 1)
        added, rejected = self.h.shelf.add_items([sparse.as_uri()])
        self.assertEqual(rejected, [])
        grant = self.h.shelf.authorize_export(added[0].id)
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.create_ticket(grant)
        self.assertEqual(ctx.exception.code, "file_too_large")
        self.assertTrue(grant.closed)


class DownloadTransferEdgeCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.h = _Harness(Path(self.tmp.name).resolve())
        self.data = bytes(index % 251 for index in range(DOWNLOAD_CHUNK_BYTES * 2 + 4321))
        self.item_id = self.h.stage("large.bin", self.data)

    def tearDown(self) -> None:
        self.h.close()
        self.tmp.cleanup()

    def test_chunk_geometry_offsets_and_digests(self):
        ticket = self.h.issue(self.item_id)
        self.assertEqual(ticket.total_chunks, 3)
        self.h.manager.claim(self.h.session, ticket.download_id)
        chunks = [
            self.h.manager.read_chunk(self.h.session, ticket.download_id, index)
            for index in range(3)
        ]
        self.assertEqual([len(c.data) for c in chunks], [DOWNLOAD_CHUNK_BYTES, DOWNLOAD_CHUNK_BYTES, 4321])
        self.assertEqual([c.offset for c in chunks], [0, DOWNLOAD_CHUNK_BYTES, 2 * DOWNLOAD_CHUNK_BYTES])
        for chunk in chunks:
            self.assertEqual(chunk.sha256, hashlib.sha256(chunk.data).digest())
        self.assertEqual(b"".join(c.data for c in chunks), self.data)
        self.assertEqual(
            self.h.manager.complete(self.h.session, ticket.download_id).state,
            DownloadState.COMPLETED,
        )

    def test_out_of_order_chunk_is_rejected(self):
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.read_chunk(self.h.session, ticket.download_id, 1)
        self.assertEqual(ctx.exception.code, "chunk_out_of_order")
        self.assertEqual(ctx.exception.details["expected_chunk_index"], 0)

    def test_duplicate_chunk_is_idempotent_and_does_not_advance(self):
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        first = self.h.manager.read_chunk(self.h.session, ticket.download_id, 0)
        repeat = self.h.manager.read_chunk(self.h.session, ticket.download_id, 0)
        self.assertEqual(first.data, repeat.data)
        self.assertEqual(repeat.sha256, first.sha256)
        self.assertEqual(repeat.bytes_sent, DOWNLOAD_CHUNK_BYTES)
        self.assertEqual(repeat.next_chunk_index, 1)

    def test_chunk_before_claim_is_rejected(self):
        ticket = self.h.issue(self.item_id)
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.read_chunk(self.h.session, ticket.download_id, 0)
        self.assertEqual(ctx.exception.code, "download_not_claimed")

    def test_invalid_chunk_index_is_rejected(self):
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        for bad in (-1, 3, 2**40, True, "0"):
            with self.assertRaises(DownloadError) as ctx:
                self.h.manager.read_chunk(self.h.session, ticket.download_id, bad)
            self.assertIn(
                ctx.exception.code,
                ("invalid_chunk_index",),
                f"accepted invalid index {bad!r}",
            )

    def test_completion_before_all_chunks_is_rejected(self):
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        self.h.manager.read_chunk(self.h.session, ticket.download_id, 0)
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.complete(self.h.session, ticket.download_id)
        self.assertEqual(ctx.exception.code, "download_incomplete")

    def test_cancel_prevents_further_chunks(self):
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        cancelled = self.h.manager.cancel(self.h.session, ticket.download_id)
        self.assertEqual(cancelled.state, DownloadState.CANCELLED)
        self.assertIsNone(ticket.grant)
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.read_chunk(self.h.session, ticket.download_id, 0)
        self.assertEqual(ctx.exception.code, "invalid_download_state")

    def test_idle_stream_is_failed(self):
        ticket = self.h.issue(self.item_id)
        self.h.manager.claim(self.h.session, ticket.download_id)
        self.h.now += 31.0
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.read_chunk(self.h.session, ticket.download_id, 0)
        self.assertEqual(ctx.exception.code, "download_idle_timeout")
        self.assertEqual(ticket.state, DownloadState.FAILED)
        self.assertIsNone(ticket.grant)

    def test_zero_length_file_completes_without_chunks(self):
        empty_id = self.h.stage("empty.bin", b"")
        ticket = self.h.issue(empty_id)
        self.assertEqual(ticket.total_chunks, 0)
        self.h.manager.claim(self.h.session, ticket.download_id)
        completed = self.h.manager.complete(self.h.session, ticket.download_id)
        self.assertEqual(completed.state, DownloadState.COMPLETED)
        self.assertEqual(completed.sha256, hashlib.sha256(b"").digest())

    def test_second_concurrent_claim_is_allowed_but_third_is_not(self):
        ids = [self.h.stage(f"f{index}.bin", b"x" * (10 + index)) for index in range(3)]
        tickets = [self.h.issue(item_id) for item_id in ids]
        self.h.manager.claim(self.h.session, tickets[0].download_id)
        self.h.manager.claim(self.h.session, tickets[1].download_id)
        with self.assertRaises(DownloadError) as ctx:
            self.h.manager.claim(self.h.session, tickets[2].download_id)
        self.assertEqual(ctx.exception.code, "concurrent_download_limit")

    def test_per_session_ticket_limit(self):
        tickets = [self.h.issue(self.item_id) for _ in range(8)]
        self.assertEqual(len(tickets), 8)
        with self.assertRaises(DownloadError) as ctx:
            self.h.issue(self.item_id)
        self.assertEqual(ctx.exception.code, "session_download_limit")

    def test_registry_bound_rejects_new_tickets(self):
        with patch("kivo.bridge.download.MAX_ACTIVE_DOWNLOADS_PER_SESSION", 100):
            for _ in range(32):
                self.h.issue(self.item_id)
            with self.assertRaises(DownloadError) as ctx:
                self.h.issue(self.item_id)
        self.assertEqual(ctx.exception.code, "download_registry_full")


if __name__ == "__main__":
    unittest.main()
