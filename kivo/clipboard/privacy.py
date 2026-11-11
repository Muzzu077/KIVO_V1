"""Privacy filter for clipboard text and metadata.

Identifies sensitive contents (passwords, tokens, private keys) and assigns
confidence levels: normal, suspicious, or sensitive.
"""
from __future__ import annotations

import math
import re
from typing import Sequence

from kivo.clipboard.models import PrivacyLevel

_SENSITIVE_MIME_HINTS = (
    "x-kde-passwordmanagerhint",
    "application/x-secret",
    "password",
)

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE
)
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
_AWS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_GITHUB_TOKEN_RE = re.compile(
    r"(?:ghp_[A-Za-z0-9_]{36}|github_pat_[A-Za-z0-9_]{82})"
)
_GITLAB_TOKEN_RE = re.compile(r"glpat-[A-Za-z0-9_-]{20}")
_SLACK_TOKEN_RE = re.compile(r"xox[baprs]-[0-9a-zA-Z]{10,48}")
_BEARER_AUTH_RE = re.compile(
    r"(?:bearer\s+|token\s+)[A-Za-z0-9._~+/-]{24,}", re.IGNORECASE
)
_BASIC_AUTH_URI_RE = re.compile(
    r"https?://[a-zA-Z0-9_.%-]+:[^@\s]{4,}@[a-zA-Z0-9.-]+"
)
_PASSWORD_PREFIX_RE = re.compile(
    r"(?:password|passwd|secret|api_key|token)[\s:=]+([^\s]{8,})", re.IGNORECASE
)


def calculate_shannon_entropy(text: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not text:
        return 0.0
    frequencies = {}
    for char in text:
        frequencies[char] = frequencies.get(char, 0) + 1
    entropy = 0.0
    length = len(text)
    for count in frequencies.values():
        p = count / length
        entropy -= p * math.log2(p)
    return entropy


class ClipboardPrivacyFilter:
    """Multi-layer privacy heuristic analyzer for clipboard entries."""

    def evaluate(
        self,
        text: str,
        mime_types: Sequence[str] | None = None,
    ) -> tuple[PrivacyLevel, bool]:
        """Analyze text and MIME hints.

        Returns:
            (PrivacyLevel, is_sensitive: bool)
        """
        if not text:
            return PrivacyLevel.NORMAL, False

        # 1. Inspect explicit MIME hints from password managers
        if mime_types:
            for mime in mime_types:
                lowered = mime.lower()
                if any(hint in lowered for hint in _SENSITIVE_MIME_HINTS):
                    return PrivacyLevel.SENSITIVE, True

        # 2. Known high-confidence credential / secret patterns
        if (
            _PRIVATE_KEY_RE.search(text)
            or _JWT_RE.search(text)
            or _AWS_KEY_RE.search(text)
            or _GITHUB_TOKEN_RE.search(text)
            or _GITLAB_TOKEN_RE.search(text)
            or _SLACK_TOKEN_RE.search(text)
            or _BASIC_AUTH_URI_RE.search(text)
            or _BEARER_AUTH_RE.search(text)
            or _PASSWORD_PREFIX_RE.search(text)
        ):
            return PrivacyLevel.SENSITIVE, True

        # 3. High-entropy candidate evaluation
        # Single-token strings with length between 20 and 80 chars
        stripped = text.strip()
        if " " not in stripped and 20 <= len(stripped) <= 80:
            entropy = calculate_shannon_entropy(stripped)
            # Mixed character classes: uppercase, lowercase, digits, symbols
            has_upper = any(c.isupper() for c in stripped)
            has_lower = any(c.islower() for c in stripped)
            has_digit = any(c.isdigit() for c in stripped)
            has_special = any(not c.isalnum() for c in stripped)
            classes_count = sum([has_upper, has_lower, has_digit, has_special])

            if entropy > 4.5 and classes_count >= 3:
                # Suspicious high-entropy single token (e.g. password or secret token)
                return PrivacyLevel.SUSPICIOUS, False
            if entropy > 5.0 and classes_count >= 2:
                return PrivacyLevel.SUSPICIOUS, False

        return PrivacyLevel.NORMAL, False

    @staticmethod
    def mask_sensitive_preview(preview: str) -> str:
        """Produce a safe masked preview for sensitive entries."""
        return "••••••••••••"


__all__ = ["ClipboardPrivacyFilter", "calculate_shannon_entropy"]
