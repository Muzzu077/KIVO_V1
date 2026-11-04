"""Path and directory validation for terminal execution."""
from __future__ import annotations

import os
from pathlib import Path

from kivo.agent.terminal.command_schemas import PathValidationError


def validate_working_directory(dir_str: str | None) -> Path:
    """Validate and resolve a safe working directory.

    Defaults to the user's home directory if None or empty string.
    Ensures the path exists, is a directory, and is accessible to the user.
    """
    raw = (dir_str or "").strip()
    if not raw:
        return Path.home().resolve()

    if "\x00" in raw:
        raise PathValidationError("Path contains illegal null bytes.")

    try:
        resolved = Path(raw).expanduser().resolve()
    except Exception as exc:
        raise PathValidationError(f"Invalid path syntax: '{raw}' ({exc})") from exc

    if not resolved.exists():
        raise PathValidationError(f"Working directory does not exist: '{raw}'")

    if not resolved.is_dir():
        raise PathValidationError(f"Working directory path is not a directory: '{raw}'")

    if not os.access(resolved, os.R_OK | os.X_OK):
        raise PathValidationError(f"Working directory is not accessible/readable: '{raw}'")

    return resolved


def sanitize_path_argument(arg: str) -> str:
    """Validate an argument that represents a file or directory path."""
    clean = (arg or "").strip()
    if "\x00" in clean:
        raise PathValidationError("Path argument contains illegal null bytes.")
    if any(c in clean for c in ("\n", "\r", "\t")):
        raise PathValidationError("Path argument contains whitespace control characters.")
    return clean
