"""Unit and adversarial tests for ClipboardPrivacyFilter."""
from __future__ import annotations

import unittest

from kivo.clipboard.models import PrivacyLevel
from kivo.clipboard.privacy import (
    ClipboardPrivacyFilter,
    calculate_shannon_entropy,
)


class ClipboardPrivacyFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.filter = ClipboardPrivacyFilter()

    # ── MIME metadata hints ───────────────────────────────────────────────
    def test_mime_hint_password_manager(self):
        level, sensitive = self.filter.evaluate(
            "supersecret123", mime_types=["x-kde-passwordManagerHint"]
        )
        self.assertTrue(sensitive)
        self.assertEqual(level, PrivacyLevel.SENSITIVE)

    def test_mime_hint_application_secret(self):
        level, sensitive = self.filter.evaluate(
            "anycontent", mime_types=["application/x-secret"]
        )
        self.assertTrue(sensitive)
        self.assertEqual(level, PrivacyLevel.SENSITIVE)

    # ── Known Secret Patterns ──────────────────────────────────────────────
    def test_detects_private_keys(self):
        keys = [
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC...",
            "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEEI...",
            "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADAN...",
        ]
        for key_text in keys:
            level, sensitive = self.filter.evaluate(key_text)
            self.assertTrue(sensitive, f"Failed to detect: {key_text[:30]}")
            self.assertEqual(level, PrivacyLevel.SENSITIVE)

    def test_detects_jwt_tokens(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        level, sensitive = self.filter.evaluate(jwt)
        self.assertTrue(sensitive)
        self.assertEqual(level, PrivacyLevel.SENSITIVE)

    def test_detects_cloud_and_git_tokens(self):
        tokens = [
            ("AWS Key", "AKIAIOSFODNN7EXAMPLE"),
            ("GitHub Classic", "ghp_1234567890abcdefghijklmnopqrstuvwxyz"),
            ("GitHub Fine-Grained", "github_pat_" + "A" * 82),
            ("GitLab Token", "glpat-" + "A" * 20),
            ("Slack Token", "xox" + "b-123456789012-1234567890123-abcdefghijklmnopqrstuvwx"),
            ("Bearer Header", "Bearer ya29.a0AfH6SMByZ8x1v9k0l2m3n4o5p6q7r8s9"),
            ("Basic Auth URI", "https://admin:SuperSecretPass123!@gitlab.example.com/repo"),
            ("Password Field", "password: MySuperSecretPassword2026!"),
            ("API Key Field", "api_key = abcdef1234567890abcdef"),
        ]
        for label, token_str in tokens:
            level, sensitive = self.filter.evaluate(token_str)
            self.assertTrue(sensitive, f"Failed for {label}")
            self.assertEqual(level, PrivacyLevel.SENSITIVE)

    # ── High Entropy Strings ───────────────────────────────────────────────
    def test_high_entropy_classified_as_suspicious_not_sensitive(self):
        # A 32-character random string with high entropy without specific label
        random_token = "K9#mQ2$zL8!vP5*wX1@yN4&tB7^rC3(j"
        entropy = calculate_shannon_entropy(random_token)
        self.assertGreater(entropy, 4.5)
        level, sensitive = self.filter.evaluate(random_token)
        self.assertEqual(level, PrivacyLevel.SUSPICIOUS)
        # Suspicious is NOT unconditionally sensitive (no false positive secret wiping)
        self.assertFalse(sensitive)

    # ── Normal Content / False Positive Avoidance ──────────────────────────
    def test_normal_text_is_normal(self):
        ordinary = [
            "Hello, this is a normal sentence copied to the clipboard.",
            "def calculate_total(items):\n    return sum(item.price for item in items)",
            "git status --short",
            "https://github.com/google/gemini-docs/blob/main/README.md",
            "1234567890",
            "",
        ]
        for text in ordinary:
            level, sensitive = self.filter.evaluate(text)
            self.assertFalse(sensitive, f"False positive on: {text}")
            self.assertEqual(level, PrivacyLevel.NORMAL)

    # ── Masking ────────────────────────────────────────────────────────────
    def test_mask_sensitive_preview(self):
        masked = self.filter.mask_sensitive_preview("SuperSecretPassword")
        self.assertEqual(masked, "••••••••••••")


if __name__ == "__main__":
    unittest.main()
