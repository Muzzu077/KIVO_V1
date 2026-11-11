"""Quarantine storage, publication, and cleanup security tests."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kivo.bridge.quarantine import (
    DISK_SAFETY_MARGIN_BYTES,
    QUARANTINE_FILE_PREFIX,
    QUARANTINE_FILE_SUFFIX,
    QUARANTINE_TIER_CACHE,
    QUARANTINE_TIER_RUNTIME,
    QUARANTINE_TIER_SPLIT_BYTES,
    TMPFS_SAFETY_MARGIN_BYTES,
    QuarantineError,
    QuarantineManager,
    Reservation,
    _candidate_names,
)
from kivo.bridge.transfer_validation import (
    MAX_AGGREGATE_RESERVED_BYTES,
    MAX_UPLOAD_BYTES,
)


def _make_quarantine(
    tmp_root: str,
    *,
    free_bytes: int = 20 * 1024 * 1024 * 1024,
    downloads_dir: str | None = None,
) -> QuarantineManager:
    runtime_base = os.path.join(tmp_root, "runtime")
    cache_base = os.path.join(tmp_root, "cache")
    os.makedirs(runtime_base, exist_ok=True)
    os.makedirs(cache_base, exist_ok=True)
    dl = downloads_dir
    if dl is None:
        dl = os.path.join(tmp_root, "Downloads")
        os.makedirs(dl, exist_ok=True)
    qm = QuarantineManager(
        runtime_base=runtime_base,
        cache_base=cache_base,
        downloads_dir=dl,
        free_space_fn=lambda root: free_bytes,
    )
    return qm


class QuarantineStartCloseTests(unittest.TestCase):

    def test_start_creates_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                self.assertTrue(qm.is_started)
                runtime_qdir = Path(tmp) / "runtime" / "kivo" / "bridge" / "quarantine"
                cache_qdir = Path(tmp) / "cache" / "kivo" / "bridge" / "quarantine"
                self.assertTrue(runtime_qdir.is_dir())
                self.assertTrue(cache_qdir.is_dir())
                self.assertEqual(stat.S_IMODE(runtime_qdir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(cache_qdir.stat().st_mode), 0o700)
            finally:
                qm.close()
            self.assertFalse(qm.is_started)

    def test_double_start_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            qm.start()
            qm.close()

    def test_double_close_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            qm.close()
            qm.close()

    def test_close_without_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.close()


class QuarantineTierTests(unittest.TestCase):

    def test_small_file_uses_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            self.assertEqual(qm.tier_for_size(0), QUARANTINE_TIER_RUNTIME)
            self.assertEqual(qm.tier_for_size(QUARANTINE_TIER_SPLIT_BYTES), QUARANTINE_TIER_RUNTIME)

    def test_large_file_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            self.assertEqual(qm.tier_for_size(QUARANTINE_TIER_SPLIT_BYTES + 1), QUARANTINE_TIER_CACHE)
            self.assertEqual(qm.tier_for_size(MAX_UPLOAD_BYTES), QUARANTINE_TIER_CACHE)


class QuarantineReservationTests(unittest.TestCase):

    def test_reserve_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                res = qm.reserve(1024)
                self.assertEqual(res.size, 1024)
                self.assertFalse(res.released)
                self.assertEqual(qm.reserved_bytes, 1024)
                qm.release(res)
                self.assertTrue(res.released)
                self.assertEqual(qm.reserved_bytes, 0)
            finally:
                qm.close()

    def test_aggregate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                r1 = qm.reserve(MAX_AGGREGATE_RESERVED_BYTES)
                with self.assertRaises(QuarantineError) as ctx:
                    qm.reserve(1)
                self.assertEqual(ctx.exception.code, "aggregate_capacity_exceeded")
                qm.release(r1)
                r2 = qm.reserve(1)
                qm.release(r2)
            finally:
                qm.close()

    def test_insufficient_free_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp, free_bytes=100)
            qm.start()
            try:
                with self.assertRaises(QuarantineError) as ctx:
                    qm.reserve(1024)
                self.assertEqual(ctx.exception.code, "insufficient_storage")
            finally:
                qm.close()

    def test_size_exceeds_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                with self.assertRaises(QuarantineError) as ctx:
                    qm.reserve(MAX_UPLOAD_BYTES + 1)
                self.assertEqual(ctx.exception.code, "size_exceeds_limit")
            finally:
                qm.close()

    def test_release_none_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            qm.release(None)
            qm.close()


class QuarantineFileTests(unittest.TestCase):

    def test_create_and_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                res = qm.reserve(100)
                handle = qm.create_file(res)
                self.assertIsNotNone(handle.fd)
                self.assertTrue(handle.name.startswith(QUARANTINE_FILE_PREFIX))
                self.assertTrue(handle.name.endswith(QUARANTINE_FILE_SUFFIX))

                data = b"hello world" * 5  # 55 bytes
                new_offset = qm.append_chunk(
                    handle, data, expected_offset=0, expected_total_size=100
                )
                self.assertEqual(new_offset, 55)

                rest = b"X" * 45
                new_offset = qm.append_chunk(
                    handle, rest, expected_offset=55, expected_total_size=100
                )
                self.assertEqual(new_offset, 100)

                qm.discard(handle)
                qm.release(res)
            finally:
                qm.close()

    def test_create_file_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                res = qm.reserve(1)
                handle = qm.create_file(res)
                st = os.fstat(handle.fd)
                self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
                qm.discard(handle)
                qm.release(res)
            finally:
                qm.close()

    def test_seal_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                data = b"test content for verification"
                size = len(data)
                sha256 = hashlib.sha256(data).digest()

                res = qm.reserve(size)
                handle = qm.create_file(res)
                qm.append_chunk(handle, data, expected_offset=0, expected_total_size=size)

                digest = qm.seal(
                    handle, expected_size=size, expected_sha256=sha256,
                    incremental_sha256=sha256,
                )
                self.assertEqual(digest, sha256)
                self.assertTrue(handle.sealed)
                self.assertIsNone(handle.fd)

                qm.discard(handle)
                qm.release(res)
            finally:
                qm.close()

    def test_seal_wrong_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                data = b"real content"
                size = len(data)
                wrong_sha256 = b"\x00" * 32

                res = qm.reserve(size)
                handle = qm.create_file(res)
                qm.append_chunk(handle, data, expected_offset=0, expected_total_size=size)

                with self.assertRaises(QuarantineError) as ctx:
                    qm.seal(handle, expected_size=size, expected_sha256=wrong_sha256)
                self.assertEqual(ctx.exception.code, "integrity_failed")
                qm.discard(handle)
                qm.release(res)
            finally:
                qm.close()

    def test_seal_wrong_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                data = b"real content"
                sha256 = hashlib.sha256(data).digest()

                res = qm.reserve(len(data))
                handle = qm.create_file(res)
                qm.append_chunk(handle, data, expected_offset=0, expected_total_size=len(data))

                with self.assertRaises(QuarantineError) as ctx:
                    qm.seal(handle, expected_size=len(data) + 10, expected_sha256=sha256)
                self.assertEqual(ctx.exception.code, "integrity_failed")
                qm.discard(handle)
                qm.release(res)
            finally:
                qm.close()


class QuarantineChunkBoundsTests(unittest.TestCase):

    def test_rejects_chunk_larger_than_1mib(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                size = 2 * 1024 * 1024
                res = qm.reserve(size)
                handle = qm.create_file(res)
                big_chunk = b"\x00" * (1024 * 1024 + 1)
                with self.assertRaises(QuarantineError) as ctx:
                    qm.append_chunk(
                        handle, big_chunk, expected_offset=0, expected_total_size=size
                    )
                self.assertEqual(ctx.exception.code, "chunk_too_large")
                qm.discard(handle)
                qm.release(res)
            finally:
                qm.close()

    def test_rejects_empty_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                res = qm.reserve(100)
                handle = qm.create_file(res)
                with self.assertRaises(QuarantineError) as ctx:
                    qm.append_chunk(handle, b"", expected_offset=0, expected_total_size=100)
                self.assertEqual(ctx.exception.code, "invalid_chunk")
                qm.discard(handle)
                qm.release(res)
            finally:
                qm.close()

    def test_rejects_chunk_past_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                res = qm.reserve(10)
                handle = qm.create_file(res)
                with self.assertRaises(QuarantineError) as ctx:
                    qm.append_chunk(handle, b"x" * 20, expected_offset=0, expected_total_size=10)
                self.assertEqual(ctx.exception.code, "chunk_length_mismatch")
                qm.discard(handle)
                qm.release(res)
            finally:
                qm.close()


class QuarantineCleanupTests(unittest.TestCase):

    def test_cleanup_removes_abandoned_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                runtime_qdir = Path(tmp) / "runtime" / "kivo" / "bridge" / "quarantine"
                abandoned = runtime_qdir / (QUARANTINE_FILE_PREFIX + "deadbeef" + QUARANTINE_FILE_SUFFIX)
                abandoned.write_bytes(b"stale data")
                os.chmod(abandoned, 0o600)

                unrelated = runtime_qdir / "unrelated_file.txt"
                unrelated.write_bytes(b"keep me")

                removed = qm.cleanup_abandoned()
                self.assertGreaterEqual(removed, 1)
                self.assertFalse(abandoned.exists())
                self.assertTrue(unrelated.exists())
            finally:
                qm.close()

    def test_cleanup_ignores_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                runtime_qdir = Path(tmp) / "runtime" / "kivo" / "bridge" / "quarantine"
                target = runtime_qdir / "real_file.txt"
                target.write_bytes(b"target data")
                symlink = runtime_qdir / (QUARANTINE_FILE_PREFIX + "sym" + QUARANTINE_FILE_SUFFIX)
                try:
                    os.symlink(str(target), str(symlink))
                except OSError:
                    self.skipTest("Cannot create symlinks")
                removed = qm.cleanup_abandoned()
                self.assertTrue(target.exists())
            finally:
                qm.close()

    def test_discard_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                res = qm.reserve(10)
                handle = qm.create_file(res)
                qm.discard(handle)
                qm.discard(handle)
                qm.discard(None)
                qm.release(res)
            finally:
                qm.close()


class QuarantinePublicationTests(unittest.TestCase):

    def test_publish_same_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                data = b"publication test content"
                size = len(data)
                sha256 = hashlib.sha256(data).digest()

                res = qm.reserve(size)
                handle = qm.create_file(res)
                qm.append_chunk(handle, data, expected_offset=0, expected_total_size=size)
                qm.seal(handle, expected_size=size, expected_sha256=sha256)

                published = qm.publish(handle, "test_download.txt", expected_sha256=sha256)
                self.assertEqual(published.name, "test_download.txt")
                self.assertEqual(published.size, size)
                self.assertTrue(handle.published)

                downloads = Path(tmp) / "Downloads"
                final = downloads / published.name
                self.assertTrue(final.exists())
                self.assertEqual(final.read_bytes(), data)

                qm.release(res)
            finally:
                qm.close()

    def test_publish_collision_avoidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = os.path.join(tmp, "Downloads")
            os.makedirs(dl, exist_ok=True)
            Path(dl, "report.pdf").write_bytes(b"existing")

            qm = _make_quarantine(tmp, downloads_dir=dl)
            qm.start()
            try:
                data = b"new file content"
                size = len(data)
                sha256 = hashlib.sha256(data).digest()

                res = qm.reserve(size)
                handle = qm.create_file(res)
                qm.append_chunk(handle, data, expected_offset=0, expected_total_size=size)
                qm.seal(handle, expected_size=size, expected_sha256=sha256)

                published = qm.publish(handle, "report.pdf", expected_sha256=sha256)
                self.assertEqual(published.name, "report (1).pdf")
                self.assertTrue((Path(dl) / "report (1).pdf").exists())
                self.assertEqual(
                    (Path(dl) / "report.pdf").read_bytes(), b"existing"
                )
                qm.release(res)
            finally:
                qm.close()

    def test_candidate_names_generator(self):
        names = list(zip(range(5), _candidate_names("photo.jpg")))
        self.assertEqual(names[0][1], "photo.jpg")
        self.assertEqual(names[1][1], "photo (1).jpg")
        self.assertEqual(names[2][1], "photo (2).jpg")

    def test_publish_rejects_unsealed(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                res = qm.reserve(10)
                handle = qm.create_file(res)
                qm.append_chunk(handle, b"0123456789", expected_offset=0, expected_total_size=10)
                with self.assertRaises(QuarantineError) as ctx:
                    qm.publish(handle, "test.txt", expected_sha256=hashlib.sha256(b"0123456789").digest())
                self.assertEqual(ctx.exception.code, "invalid_transfer_state")
                qm.discard(handle)
                qm.release(res)
            finally:
                qm.close()

    def test_empty_file_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            qm = _make_quarantine(tmp)
            qm.start()
            try:
                data = b""
                size = 0
                sha256 = hashlib.sha256(data).digest()

                res = qm.reserve(size)
                handle = qm.create_file(res)
                digest = qm.seal(handle, expected_size=size, expected_sha256=sha256)
                self.assertEqual(digest, sha256)

                published = qm.publish(handle, "empty.txt", expected_sha256=sha256)
                self.assertEqual(published.name, "empty.txt")
                self.assertEqual(published.size, 0)

                final = Path(tmp) / "Downloads" / "empty.txt"
                self.assertTrue(final.exists())
                self.assertEqual(final.stat().st_size, 0)

                qm.release(res)
            finally:
                qm.close()


class QuarantineRootSecurityTests(unittest.TestCase):

    def test_rejects_symlinked_quarantine_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_dir = os.path.join(tmp, "real_runtime")
            os.makedirs(real_dir)
            link_dir = os.path.join(tmp, "link_runtime")
            try:
                os.symlink(real_dir, link_dir)
            except OSError:
                self.skipTest("Cannot create symlinks")

            qm = QuarantineManager(
                runtime_base=link_dir,
                cache_base=os.path.join(tmp, "cache"),
                downloads_dir=os.path.join(tmp, "dl"),
                free_space_fn=lambda r: 10 * 1024**3,
            )
            os.makedirs(os.path.join(tmp, "cache"), exist_ok=True)
            os.makedirs(os.path.join(tmp, "dl"), exist_ok=True)
            with self.assertRaises(QuarantineError):
                qm.start()


if __name__ == "__main__":
    unittest.main()
