"""Unit and adversarial tests for URI parsing and descriptor-relative path authorization."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from kivo.files.workspace import WorkspaceConfig, WorkspaceRoot
from kivo.shelf.models import MAX_URI_BYTES, sanitize_filename
from kivo.shelf.path_policy import (
    PathAuthorizer,
    ShelfPolicyError,
    parse_and_validate_file_uri,
)


class URIParsingTests(unittest.TestCase):
    # ── Valid Local File URIs ──────────────────────────────────────────────
    def test_valid_file_uri(self):
        path = parse_and_validate_file_uri("file:///home/user/document.pdf")
        self.assertEqual(path, "/home/user/document.pdf")

    def test_encoded_spaces_and_unicode(self):
        path = parse_and_validate_file_uri("file:///home/user/my%20notes%20%E2%9C%93.txt")
        self.assertEqual(path, "/home/user/my notes ✓.txt")

    def test_localhost_authority_allowed(self):
        path = parse_and_validate_file_uri("file://localhost/home/user/file.txt")
        self.assertEqual(path, "/home/user/file.txt")

    # ── Rejected Schemes and Remote Authorities ────────────────────────────
    def test_unsupported_schemes_rejected(self):
        for bad_uri in (
            "http://example.com/file.txt",
            "https://example.com/file.txt",
            "ftp://files.example.com/file.txt",
            "smb://server/share/file.txt",
            "custom://app/file.txt",
            "/just/a/path/file.txt",
        ):
            with self.assertRaises(ShelfPolicyError) as raised:
                parse_and_validate_file_uri(bad_uri)
            self.assertEqual(raised.exception.code, "unsupported_scheme")

    def test_remote_authorities_rejected(self):
        for remote in (
            "file://remote-host/share/file.txt",
            "file://192.168.1.1/share/file.txt",
            "file://evil.com/exploit.pdf",
        ):
            with self.assertRaises(ShelfPolicyError) as raised:
                parse_and_validate_file_uri(remote)
            self.assertEqual(raised.exception.code, "remote_authority_rejected")

    # ── Double-Encoding & Traversal ────────────────────────────────────────
    def test_double_encoded_traversal_rejected(self):
        # %252e%252e is double-encoded '..'
        double_traversal = "file:///home/user/%252e%252e/%252e%252e/etc/passwd"
        with self.assertRaises(ShelfPolicyError) as raised:
            parse_and_validate_file_uri(double_traversal)
        self.assertEqual(raised.exception.code, "traversal_detected")

    # ── Invalid Characters and Length ──────────────────────────────────────
    def test_null_bytes_rejected(self):
        with self.assertRaises(ShelfPolicyError) as raised:
            parse_and_validate_file_uri("file:///home/user/file\x00.txt")
        self.assertEqual(raised.exception.code, "invalid_uri")

    def test_bidi_overrides_rejected(self):
        # U+202E Right-to-Left Override
        bidi_uri = "file:///home/user/doc\u202epdf.exe"
        with self.assertRaises(ShelfPolicyError) as raised:
            parse_and_validate_file_uri(bidi_uri)
        self.assertEqual(raised.exception.code, "invalid_uri")

    def test_control_chars_rejected(self):
        with self.assertRaises(ShelfPolicyError) as raised:
            parse_and_validate_file_uri("file:///home/user/file\x1b[31m.txt")
        self.assertEqual(raised.exception.code, "invalid_uri")

    def test_oversized_uri_rejected(self):
        huge_uri = "file:///home/user/" + "a" * (MAX_URI_BYTES + 10)
        with self.assertRaises(ShelfPolicyError) as raised:
            parse_and_validate_file_uri(huge_uri)
        self.assertEqual(raised.exception.code, "uri_too_long")


class PathAuthorizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ws_dir = self.root / "workspace"
        self.ws_dir.mkdir(parents=True)
        self.external_dir = self.root / "external"
        self.external_dir.mkdir(parents=True)

        ws_root = WorkspaceRoot(
            id="test-ws",
            path=self.ws_dir,
            mutation_enabled=False,
        )
        self.config = WorkspaceConfig(
            roots={"test-ws": ws_root},
        )
        self.authorizer = PathAuthorizer(workspace_config=self.config)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ── Approved Workspace File ────────────────────────────────────────────
    def test_workspace_file_authorized(self):
        target = self.ws_dir / "report.pdf"
        target.write_bytes(b"%PDF-1.4 sample content")
        canonical, identity, is_ws, ws_name = self.authorizer.authorize_and_inspect(str(target))
        self.assertEqual(canonical, str(target))
        self.assertTrue(is_ws)
        self.assertEqual(ws_name, "test-ws")
        self.assertEqual(identity.size, len(b"%PDF-1.4 sample content"))
        self.assertTrue(stat.S_ISREG(identity.mode))

    # ── External File Authorized Safely ────────────────────────────────────
    def test_external_file_authorized(self):
        target = self.external_dir / "photo.png"
        target.write_bytes(b"\x89PNG sample photo")
        canonical, identity, is_ws, ws_name = self.authorizer.authorize_and_inspect(str(target))
        self.assertEqual(canonical, str(target))
        self.assertFalse(is_ws)
        self.assertIsNone(ws_name)
        self.assertEqual(identity.size, len(b"\x89PNG sample photo"))

    # ── Symlinks Strictly Rejected ─────────────────────────────────────────
    def test_final_symlink_rejected(self):
        real_file = self.ws_dir / "real.txt"
        real_file.write_text("content")
        link_file = self.ws_dir / "link_to_real.txt"
        link_file.symlink_to(real_file)

        with self.assertRaises(ShelfPolicyError) as raised:
            self.authorizer.authorize_and_inspect(str(link_file))
        self.assertEqual(raised.exception.code, "symlink_not_allowed")

    def test_parent_component_symlink_rejected(self):
        sub = self.ws_dir / "sub"
        sub.mkdir()
        real_file = sub / "file.txt"
        real_file.write_text("content")

        symlink_dir = self.ws_dir / "sym_dir"
        symlink_dir.symlink_to(sub)
        target = symlink_dir / "file.txt"

        with self.assertRaises(ShelfPolicyError) as raised:
            self.authorizer.authorize_and_inspect(str(target))
        self.assertEqual(raised.exception.code, "symlink_not_allowed")

    # ── Directories and Non-Regular Files Rejected ─────────────────────────
    def test_directory_rejected(self):
        with self.assertRaises(ShelfPolicyError) as raised:
            self.authorizer.authorize_and_inspect(str(self.ws_dir))
        self.assertEqual(raised.exception.code, "unsupported_file_type")

    def test_missing_file_rejected(self):
        with self.assertRaises(ShelfPolicyError) as raised:
            self.authorizer.authorize_and_inspect(str(self.ws_dir / "nonexistent.txt"))
        self.assertEqual(raised.exception.code, "file_not_found")

    # ── System Directories and Sensitive Heuristics ────────────────────────
    def test_system_directories_rejected(self):
        for sys_path in ("/etc/passwd", "/proc/cpuinfo", "/sys/class/hwmon"):
            with self.assertRaises(ShelfPolicyError) as raised:
                self.authorizer.authorize_and_inspect(sys_path)
            self.assertEqual(raised.exception.code, "system_path_denied")

    def test_sensitive_path_components_rejected(self):
        for sens_part in (".ssh", ".gnupg", ".env"):
            sens_dir = self.ws_dir / sens_part
            sens_dir.mkdir(parents=True)
            sens_file = sens_dir / "id_rsa"
            sens_file.write_text("key")
            with self.assertRaises(ShelfPolicyError) as raised:
                self.authorizer.authorize_and_inspect(str(sens_file))
            self.assertEqual(raised.exception.code, "sensitive_path_denied")

    # ── Identity Verification and Replacement Detection ───────────────────
    def test_verify_identity_detects_replaced_file(self):
        target = self.ws_dir / "versioned.txt"
        target.write_text("version 1")
        _, identity1, _, _ = self.authorizer.authorize_and_inspect(str(target))

        # Replace file by unlinking and recreating (new inode)
        target.unlink()
        target.write_text("version 2")
        valid = self.authorizer.verify_existing_identity(str(target), identity1)
        self.assertFalse(valid)

    def test_verify_identity_detects_deleted_file(self):
        target = self.ws_dir / "temp.txt"
        target.write_text("temp")
        _, identity, _, _ = self.authorizer.authorize_and_inspect(str(target))
        target.unlink()
        valid = self.authorizer.verify_existing_identity(str(target), identity)
        self.assertFalse(valid)


if __name__ == "__main__":
    unittest.main()
