"""WirePlumber command execution and output parsing.

Wraps ``wpctl`` through ``kivo.core.bounded_process.run_bounded``.  Every call
uses fixed argv arrays, no shell, strict timeouts, and capped buffers.  Zero
exit with empty stdout (or 'Node not found' on stderr) is treated as a failure.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from typing import Callable, Sequence

from kivo.core.bounded_process import BoundedProcessResult, run_bounded
from kivo.microphone.models import (
    ERROR_COMMAND_FAILED,
    ERROR_COMMAND_TIMEOUT,
    ERROR_INVALID_OUTPUT,
    ERROR_NO_DEFAULT_SOURCE,
    ERROR_WIREPLUMBER_UNAVAILABLE,
    ERROR_WPCTL_MISSING,
    MicSource,
)

_COMMAND_TIMEOUT = 0.75
_MAX_STDOUT_BYTES = 16 * 1024
_MAX_STDERR_BYTES = 4 * 1024
_MAX_LINES = 256
_NODE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_VOLUME_PATTERN = re.compile(
    r"^Volume:\s+([0-9]+(?:\.[0-9]+)?)(?:\s+\[MUTED\])?\s*$"
)


@dataclass(frozen=True)
class VolumeReading:
    """Result of reading volume and mute state for an audio node."""

    volume: float
    muted: bool


_UNSET = object()


class WpctlRunner:
    """Bounded, argv-only runner for WirePlumber CLI operations."""

    def __init__(
        self,
        wpctl_path: str | None | object = _UNSET,
        *,
        runner: Callable[..., BoundedProcessResult] = run_bounded,
        timeout: float = _COMMAND_TIMEOUT,
    ):
        self._runner = runner
        self._timeout = float(timeout)
        self._wpctl_path = self._resolve_wpctl(wpctl_path)

    @staticmethod
    def _resolve_wpctl(path_override: str | None | object) -> str | None:
        if path_override is _UNSET:
            candidate = shutil.which("wpctl")
        elif path_override is None:
            candidate = None
        else:
            candidate = str(path_override)

        if not candidate:
            return None
        resolved = os.path.abspath(candidate)
        if os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return resolved
        return None

    @property
    def is_available(self) -> bool:
        return self._wpctl_path is not None

    # ── Source Discovery ───────────────────────────────────────────────────

    def list_default_source(self) -> tuple[MicSource | None, str | None, str | None]:
        """Resolve the default audio source from ``wpctl list audio sources``.

        Returns:
            (MicSource, None, None) on success.
            (None, error_code, error_message) on failure.
        """
        if self._wpctl_path is None:
            return None, ERROR_WPCTL_MISSING, "wpctl executable not found or not executable"

        argv = [self._wpctl_path, "list", "audio", "sources"]
        result = self._execute(argv)

        if result.timed_out:
            return None, ERROR_COMMAND_TIMEOUT, "wpctl list audio sources timed out"
        if result.returncode != 0:
            msg = result.stderr.strip() or f"exit code {result.returncode}"
            return None, ERROR_COMMAND_FAILED, f"wpctl list audio sources failed: {msg}"
        if not result.stdout.strip():
            return None, ERROR_INVALID_OUTPUT, "wpctl list audio sources returned empty output"

        lines = result.stdout.splitlines()[:_MAX_LINES]
        default_candidates: list[MicSource] = []

        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue
            parts = line.split("\t")
            if len(parts) not in (3, 4):
                # Try whitespace-split fallback only if tab split produced 1 element
                if len(parts) == 1:
                    parts = line_str.split(None, 3)
                if len(parts) not in (3, 4):
                    continue

            raw_id = parts[0].strip()
            raw_name = parts[1].strip()
            raw_class = parts[2].strip()
            is_def_marker = parts[3].strip() if len(parts) >= 4 else ""

            try:
                node_id = int(raw_id)
            except ValueError:
                continue
            if node_id <= 0 or node_id > 2147483647:
                continue

            if not _NODE_NAME_PATTERN.match(raw_name):
                continue
            if raw_class != "audio/source":
                continue

            is_default = (is_def_marker == "*")
            source = MicSource(
                id=node_id,
                name=raw_name,
                description=self._friendly_description(raw_name),
                is_default=is_default,
            )
            if is_default:
                default_candidates.append(source)

        if len(default_candidates) == 0:
            return None, ERROR_NO_DEFAULT_SOURCE, "No default audio source found with '*' marker"
        if len(default_candidates) > 1:
            return None, ERROR_INVALID_OUTPUT, f"Multiple ({len(default_candidates)}) default audio sources marked with '*'"

        return default_candidates[0], None, None

    # ── Volume and Mute Reading ────────────────────────────────────────────

    def get_volume(
        self, target: str | int
    ) -> tuple[VolumeReading | None, str | None, str | None]:
        """Read volume and mute state for ``target`` (numeric ID or alias).

        Returns:
            (VolumeReading, None, None) on success.
            (None, error_code, error_message) on failure.
        """
        if self._wpctl_path is None:
            return None, ERROR_WPCTL_MISSING, "wpctl executable not found or not executable"

        if isinstance(target, int):
            if target <= 0 or target > 2147483647:
                return None, ERROR_INVALID_OUTPUT, f"Invalid target node ID: {target}"
            target_str = str(target)
        else:
            target_str = str(target).strip()
            if not target_str:
                return None, ERROR_INVALID_OUTPUT, "Empty target specification"

        argv = [self._wpctl_path, "get-volume", target_str]
        result = self._execute(argv)

        if result.timed_out:
            return None, ERROR_COMMAND_TIMEOUT, f"wpctl get-volume {target_str} timed out"

        # Special WirePlumber case: wpctl get-volume exits 0 on nonexistent nodes
        # while writing "Node '<id>' not found" to stderr and empty stdout.
        stdout_stripped = result.stdout.strip()
        stderr_stripped = result.stderr.strip()

        if not stdout_stripped:
            if "not found" in stderr_stripped.lower():
                return None, ERROR_INVALID_OUTPUT, f"Node '{target_str}' not found"
            if result.returncode != 0:
                return None, ERROR_COMMAND_FAILED, f"wpctl get-volume failed: {stderr_stripped or result.returncode}"
            return None, ERROR_INVALID_OUTPUT, f"wpctl get-volume {target_str} returned empty stdout"

        if result.returncode != 0:
            return None, ERROR_COMMAND_FAILED, f"wpctl get-volume exited {result.returncode}: {stderr_stripped}"

        # Match single volume line
        first_line = stdout_stripped.splitlines()[0].strip()
        match = _VOLUME_PATTERN.match(first_line)
        if not match:
            return None, ERROR_INVALID_OUTPUT, f"Malformed volume output: {first_line!r}"

        try:
            vol = float(match.group(1))
        except (ValueError, TypeError):
            return None, ERROR_INVALID_OUTPUT, f"Non-numeric volume: {match.group(1)!r}"

        is_muted = "[MUTED]" in first_line
        return VolumeReading(volume=vol, muted=is_muted), None, None

    # ── Set Mute ───────────────────────────────────────────────────────────

    def set_mute(
        self, node_id: int, mute: bool
    ) -> tuple[bool, str | None, str | None]:
        """Apply explicit mute (1) or unmute (0) to a numeric node ID.

        Returns:
            (True, None, None) on command exit 0.
            (False, error_code, error_message) on failure.
        """
        if self._wpctl_path is None:
            return False, ERROR_WPCTL_MISSING, "wpctl executable not found or not executable"

        if not isinstance(node_id, int) or node_id <= 0 or node_id > 2147483647:
            return False, ERROR_INVALID_OUTPUT, f"Invalid node ID for set-mute: {node_id}"

        flag = "1" if mute else "0"
        argv = [self._wpctl_path, "set-mute", str(node_id), flag]
        result = self._execute(argv)

        if result.timed_out:
            return False, ERROR_COMMAND_TIMEOUT, f"wpctl set-mute {node_id} {flag} timed out"

        if result.returncode != 0:
            msg = result.stderr.strip() or f"exit code {result.returncode}"
            return False, ERROR_COMMAND_FAILED, f"wpctl set-mute {node_id} {flag} failed: {msg}"

        return True, None, None

    # ── Helpers ────────────────────────────────────────────────────────────

    def _execute(self, argv: Sequence[str]) -> BoundedProcessResult:
        try:
            return self._runner(
                argv,
                timeout=self._timeout,
                stdout_limit=_MAX_STDOUT_BYTES,
                stderr_limit=_MAX_STDERR_BYTES,
                env={"LC_ALL": "C", "LANG": "C"},
            )
        except OSError as exc:
            return BoundedProcessResult(
                returncode=1,
                stdout="",
                stderr=str(exc),
            )

    @staticmethod
    def _friendly_description(node_name: str) -> str:
        """Derive a clean, display-friendly microphone label without hardware paths."""
        lowered = node_name.lower()
        if "mic1" in lowered or "internal" in lowered or "built-in" in lowered:
            return "Internal Microphone"
        if "headset" in lowered:
            return "Headset Microphone"
        if "usb" in lowered:
            return "USB Microphone"
        return "Default microphone"


__all__ = ["WpctlRunner", "VolumeReading"]
