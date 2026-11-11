"""Unit and adversarial tests for upload metadata validation."""
from __future__ import annotations

import unittest

from kivo.bridge.transfer_validation import (
    BIDI_CONTROL_CODEPOINTS,
    MAX_FILENAME_CHARS,
    MAX_UPLOAD_BYTES,
    TRANSFER_DECISIONS,
    UploadValidationError,
    validate_declared_size,
    validate_decision,
    validate_display_filename,
    validate_mime_hint,
    validate_sha256_hex,
    validate_transfer_id,
)


class FilenameValidationTests(unittest.TestCase):

    def test_valid_ascii(self):
        self.assertEqual(validate_display_filename("report.pdf"), "report.pdf")

    def test_valid_unicode(self):
        for name in ("résumé.docx", "日本語ファイル.txt", "emoji 📎.txt"):
            result = validate_display_filename(name)
            self.assertIsInstance(result, str)
            self.assertEqual(len(result), len(name))

    def test_nfc_normalization(self):
        import unicodedata
        composed = "café"
        decomposed = unicodedata.normalize("NFD", composed)
        result = validate_display_filename(decomposed)
        self.assertEqual(result, unicodedata.normalize("NFC", decomposed))

    def test_rejects_empty(self):
        with self.assertRaises(UploadValidationError) as ctx:
            validate_display_filename("")
        self.assertEqual(ctx.exception.code, "invalid_filename")

    def test_rejects_non_string(self):
        for bad in (None, 123, b"file.txt", ["file.txt"]):
            with self.assertRaises(UploadValidationError):
                validate_display_filename(bad)

    def test_rejects_path_separators(self):
        for bad in ("dir/file.txt", "dir\\file.txt", "../etc/passwd"):
            with self.assertRaises(UploadValidationError):
                validate_display_filename(bad)

    def test_rejects_nul(self):
        with self.assertRaises(UploadValidationError):
            validate_display_filename("file\x00name.txt")

    def test_rejects_control_chars(self):
        for char in ("\x01", "\x1f", "\x7f", "\x80", "\x9f"):
            with self.assertRaises(UploadValidationError):
                validate_display_filename(f"file{char}name.txt")

    def test_rejects_bidi_controls(self):
        for cp in sorted(BIDI_CONTROL_CODEPOINTS)[:5]:
            with self.assertRaises(UploadValidationError):
                validate_display_filename(f"file{chr(cp)}name.txt")

    def test_rejects_dot_and_dotdot(self):
        for bad in (".", ".."):
            with self.assertRaises(UploadValidationError):
                validate_display_filename(bad)

    def test_rejects_leading_trailing_whitespace(self):
        for bad in (" file.txt", "file.txt ", " file.txt "):
            with self.assertRaises(UploadValidationError):
                validate_display_filename(bad)

    def test_rejects_too_long(self):
        with self.assertRaises(UploadValidationError):
            validate_display_filename("a" * (MAX_FILENAME_CHARS + 1))

    def test_max_length_accepted(self):
        name = "a" * MAX_FILENAME_CHARS
        self.assertEqual(validate_display_filename(name), name)


class SizeValidationTests(unittest.TestCase):

    def test_valid_sizes(self):
        for size in (0, 1, 1024, MAX_UPLOAD_BYTES):
            self.assertEqual(validate_declared_size(size), size)

    def test_rejects_negative(self):
        with self.assertRaises(UploadValidationError):
            validate_declared_size(-1)

    def test_rejects_too_large(self):
        with self.assertRaises(UploadValidationError):
            validate_declared_size(MAX_UPLOAD_BYTES + 1)

    def test_rejects_non_int(self):
        for bad in ("100", 1.5, True, False, None):
            with self.assertRaises(UploadValidationError):
                validate_declared_size(bad)


class SHA256ValidationTests(unittest.TestCase):

    def test_valid_hex(self):
        hex_str = "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e"
        result = validate_sha256_hex(hex_str)
        self.assertEqual(len(result), 32)
        self.assertEqual(result, bytes.fromhex(hex_str))

    def test_rejects_short(self):
        with self.assertRaises(UploadValidationError):
            validate_sha256_hex("a591a6")

    def test_rejects_non_hex(self):
        with self.assertRaises(UploadValidationError):
            validate_sha256_hex("g" * 64)

    def test_rejects_empty(self):
        with self.assertRaises(UploadValidationError):
            validate_sha256_hex("")

    def test_rejects_non_string(self):
        with self.assertRaises(UploadValidationError):
            validate_sha256_hex(None)


class MIMEValidationTests(unittest.TestCase):

    def test_valid_types(self):
        for mime in (
            "application/pdf",
            "image/jpeg",
            "text/plain; charset=utf-8",
            "application/octet-stream",
        ):
            self.assertEqual(validate_mime_hint(mime), mime)

    def test_none_returns_none(self):
        self.assertIsNone(validate_mime_hint(None))

    def test_empty_returns_none(self):
        self.assertIsNone(validate_mime_hint(""))

    def test_rejects_control_chars(self):
        with self.assertRaises(UploadValidationError):
            validate_mime_hint("text/\x00plain")

    def test_rejects_non_string(self):
        with self.assertRaises(UploadValidationError):
            validate_mime_hint(123)


class TransferIDValidationTests(unittest.TestCase):

    def test_valid(self):
        valid = "tr_aabbccddee112233aabbccdd"
        self.assertEqual(validate_transfer_id(valid), valid)

    def test_rejects_short(self):
        with self.assertRaises(UploadValidationError):
            validate_transfer_id("tr_aabb")

    def test_rejects_wrong_prefix(self):
        with self.assertRaises(UploadValidationError):
            validate_transfer_id("xx_aabbccddee112233aabbccdd")

    def test_rejects_uppercase(self):
        with self.assertRaises(UploadValidationError):
            validate_transfer_id("tr_AABBCCDDEE112233AABBCCDD")


class DecisionValidationTests(unittest.TestCase):

    def test_valid_decisions(self):
        for d in TRANSFER_DECISIONS:
            self.assertEqual(validate_decision(d), d)

    def test_rejects_unknown(self):
        with self.assertRaises(UploadValidationError):
            validate_decision("force_accept")

    def test_rejects_non_string(self):
        with self.assertRaises(UploadValidationError):
            validate_decision(None)


if __name__ == "__main__":
    unittest.main()
