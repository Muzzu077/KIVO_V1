"""Service-level tests for explicit shelf export authorization and downloads."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from kivo.bridge.models import PairedSession
from kivo.bridge.service import PhoneBridgeService
from kivo.shelf.path_policy import PathAuthorizer
from kivo.shelf.service import FileShelfService


def _make_session() -> PairedSession:
    now = time.time()
    return PairedSession(
        session_id="sess_service_download",
        client_id="client_service_download",
        device_name="Service Phone",
        client_ip="127.0.0.1",
        bearer_token="sess_tok_service_download",
        upstream_key=os.urandom(32),
        downstream_key=os.urandom(32),
        created_at=now,
        last_active=now,
        expires_at=now + 3600.0,
    )


class DownloadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "export.txt"
        self.data = b"service export payload"
        self.path.write_bytes(self.data)

        self.shelf = FileShelfService(
            authorizer=PathAuthorizer(), xdg_open_path="/bin/true"
        )
        self.shelf.start()
        added, rejected = self.shelf.add_items([self.path.as_uri()])
        self.assertEqual(rejected, [])
        self.item_id = added[0].id

        self.events: list[tuple[str, dict]] = []
        self.service = PhoneBridgeService(
            on_transfer_event=lambda event, payload: self.events.append((event, payload)),
            shelf_service=self.shelf,
        )
        self.session = _make_session()
        self.service._pairing_manager._active_session = self.session
        self.service._download_manager.start()

    def tearDown(self) -> None:
        self.service.close()
        self.shelf.close()
        self.tmp.cleanup()

    def test_create_ticket_requires_authorize_flag(self):
        for authorize in (False, None, "true", 1):
            result = self.service.create_download_ticket(self.item_id, authorize)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "authorization_required")
        self.assertEqual(self.service.get_downloads()["downloads"], [])

    def test_create_ticket_issues_for_bound_device(self):
        result = self.service.create_download_ticket(self.item_id, True)
        self.assertTrue(result["ok"], result)
        download = result["download"]
        self.assertRegex(download["download_id"], r"^tkt_[0-9a-f]{64}$")
        self.assertEqual(download["state"], "issued")
        self.assertEqual(download["size"], len(self.data))
        self.assertEqual(download["device_name"], "Service Phone")
        snapshot = self.service.get_downloads()
        self.assertTrue(snapshot["ok"])
        self.assertEqual(len(snapshot["downloads"]), 1)
        encoded = json.dumps(snapshot)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn(self.item_id, encoded)
        self.assertIn("bridge_download_requested", [event for event, _ in self.events])

    def test_create_ticket_without_paired_session(self):
        self.service._pairing_manager._active_session = None
        result = self.service.create_download_ticket(self.item_id, True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "no_active_session")

    def test_stale_file_cannot_be_exported(self):
        self.path.write_bytes(b"changed after staging")
        result = self.service.create_download_ticket(self.item_id, True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "stale_reference")

    def test_unknown_item_cannot_be_exported(self):
        result = self.service.create_download_ticket("shelf_000000000000", True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "item_not_found")

    def test_cancel_download_by_desktop(self):
        created = self.service.create_download_ticket(self.item_id, True)
        download_id = created["download"]["download_id"]
        cancelled = self.service.cancel_download(download_id)
        self.assertTrue(cancelled["ok"])
        self.assertEqual(cancelled["download"]["state"], "cancelled")
        again = self.service.cancel_download(download_id)
        self.assertTrue(again["ok"])
        self.assertEqual(again["download"]["state"], "cancelled")

    def test_revoke_device_revokes_download_ticket(self):
        created = self.service.create_download_ticket(self.item_id, True)
        download_id = created["download"]["download_id"]
        self.assertTrue(self.service.revoke_device(self.session.session_id))
        snapshot = self.service.get_downloads()
        ticket = next(d for d in snapshot["downloads"] if d["download_id"] == download_id)
        self.assertEqual(ticket["state"], "revoked")


if __name__ == "__main__":
    unittest.main()
