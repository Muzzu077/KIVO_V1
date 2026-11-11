"""Strict versioned download handoff tests (synthetic metadata only)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from kivo.bridge.download_handoff import (
    DOWNLOAD_HANDOFF_MAX_BYTES,
    DOWNLOAD_HANDOFF_PREFIX,
    DownloadHandoffError,
    build_download_handoff,
    parse_download_handoff,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "download_handoff_vectors.json"
TICKET = "tkt_" + "ab" * 32
DEVICE = "client_download_test"
NAME = "quarterly report & notes.pdf"
SIZE = 3_145_745
SHA = "fe2aaf82bfa2ffec207a0c6fa7ce7d4af268d67e2672fdaec675f3f9b65d0854"


class DownloadHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text())

    def test_deterministic_handoff_matches_shared_fixture(self):
        payload = build_download_handoff(
            ticket_id=TICKET,
            device_id=DEVICE,
            filename=NAME,
            size=SIZE,
            sha256_hex=SHA,
        )
        self.assertEqual(payload, self.fixture["payload"])
        self.assertTrue(payload.startswith(DOWNLOAD_HANDOFF_PREFIX))
        self.assertLessEqual(len(payload.encode("utf-8")), DOWNLOAD_HANDOFF_MAX_BYTES)
        self.assertNotIn("file://", payload)

    def test_valid_handoff_round_trips(self):
        parsed = parse_download_handoff(
            self.fixture["payload"], expected_device_id=DEVICE
        )
        self.assertEqual(parsed.ticket_id, TICKET)
        self.assertEqual(parsed.device_id, DEVICE)
        self.assertEqual(parsed.filename, NAME)
        self.assertEqual(parsed.size, SIZE)
        self.assertEqual(parsed.sha256_hex, SHA)
        self.assertEqual(parsed.version, "v1")

    def test_duplicate_parameters_are_rejected(self):
        payload = self.fixture["payload"] + "&ticket=" + TICKET
        with self.assertRaises(DownloadHandoffError) as ctx:
            parse_download_handoff(payload)
        self.assertEqual(ctx.exception.code, "duplicate_parameter")

    def test_unknown_or_missing_parameters_are_rejected(self):
        for payload in (
            self.fixture["payload"] + "&path=%2Fetc%2Fpasswd",
            DOWNLOAD_HANDOFF_PREFIX + "ticket=" + TICKET,
            DOWNLOAD_HANDOFF_PREFIX + "&".join(
                f"{key}={value}" for key, value in self.fixture["params"].items()
                if key != "device"
            ),
        ):
            with self.assertRaises(DownloadHandoffError) as ctx:
                parse_download_handoff(payload)
            self.assertEqual(ctx.exception.code, "invalid_handoff")

    def test_unsupported_version_and_scheme_are_rejected(self):
        for payload, code in (
            (self.fixture["payload"].replace("://v1?", "://v2?"), "unsupported_version"),
            (self.fixture["payload"].replace("kivo-download://", "kivo-pair://"), "invalid_handoff"),
            (self.fixture["payload"].replace("kivo-download://", "file://"), "invalid_handoff"),
            ("", "invalid_handoff"),
            ("  " + self.fixture["payload"], "invalid_handoff"),
        ):
            with self.assertRaises(DownloadHandoffError) as ctx:
                parse_download_handoff(payload)
            self.assertEqual(ctx.exception.code, code)

    def test_malformed_and_altered_tickets_are_rejected(self):
        for ticket in (
            "tkt_" + "A" * 64,
            "tkt_" + "a" * 63,
            "tkt_" + "a" * 65,
            "tr_" + "a" * 24,
            "tkt_" + "a" * 64 + "%00",
        ):
            payload = build_download_handoff(
                ticket_id=TICKET,
                device_id=DEVICE,
                filename=NAME,
                size=SIZE,
                sha256_hex=SHA,
            ).replace(TICKET, ticket)
            with self.assertRaises(DownloadHandoffError):
                parse_download_handoff(payload)

    def test_oversized_payload_is_rejected_before_parsing(self):
        payload = self.fixture["payload"] + "&padding=" + "a" * DOWNLOAD_HANDOFF_MAX_BYTES
        with self.assertRaises(DownloadHandoffError) as ctx:
            parse_download_handoff(payload)
        self.assertEqual(ctx.exception.code, "handoff_too_large")

    def test_duplicate_encoding_and_noncanonical_fields_are_rejected(self):
        params = self.fixture["params"]
        cases = (
            DOWNLOAD_HANDOFF_PREFIX + "&".join(
                f"{key}={value}" for key, value in params.items()
            ).replace("size=3145745", "size=03145745"),
            DOWNLOAD_HANDOFF_PREFIX + "&".join(
                f"{key}={value}" for key, value in params.items()
            ).replace(SHA, SHA.upper()),
            DOWNLOAD_HANDOFF_PREFIX + "&".join(
                f"{key}={value}" for key, value in params.items()
            ).replace("name=", "name=%2Fetc%2F"),
        )
        for payload in cases:
            with self.assertRaises(DownloadHandoffError):
                parse_download_handoff(payload)

    def test_percent_encoding_is_strict(self):
        base = DOWNLOAD_HANDOFF_PREFIX + "&".join(
            f"{key}={value}" for key, value in self.fixture["params"].items()
        )
        for mutated in (
            base.replace("name=", "name=%2"),
            base.replace("name=", "name=%zz"),
            base.replace("name=", "name=quarterly+report"),
            base.replace("name=", "name=quarterly%20report%00"),
        ):
            with self.assertRaises(DownloadHandoffError):
                parse_download_handoff(mutated)

    def test_device_mismatch_is_rejected(self):
        with self.assertRaises(DownloadHandoffError) as ctx:
            parse_download_handoff(
                self.fixture["payload"], expected_device_id="client_other_device"
            )
        self.assertEqual(ctx.exception.code, "handoff_device_mismatch")

    def test_size_and_digest_bounds_are_enforced(self):
        for size in ("-1", "+1", "3145745.0", str(2 * 1024**3 + 1), "000"):
            payload = DOWNLOAD_HANDOFF_PREFIX + "&".join(
                f"{key}={value}" for key, value in {
                    **self.fixture["params"],
                    "size": size,
                }.items()
            )
            with self.assertRaises(DownloadHandoffError):
                parse_download_handoff(payload)
        for digest in ("0" * 63, "0" * 65, "G" * 64):
            payload = DOWNLOAD_HANDOFF_PREFIX + "&".join(
                f"{key}={value}" for key, value in {
                    **self.fixture["params"],
                    "sha256": digest,
                }.items()
            )
            with self.assertRaises(DownloadHandoffError):
                parse_download_handoff(payload)

    def test_payload_never_contains_paths_or_secrets(self):
        payload = self.fixture["payload"]
        for forbidden in ("/home/", "/tmp/", "file://", "bearer", "key_hex", "BEGIN "):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
