"""Persistent configuration for KIVO audio input devices.

Stores the user's selected microphone in an XDG-compatible configuration
directory, using atomic writes to prevent partial files on power failure.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _get_config_dir() -> Path:
    """Return the user's XDG config directory for KIVO."""
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config and os.path.isabs(xdg_config):
        base = Path(xdg_config)
    else:
        base = Path.home() / ".config"
    config_dir = base / "kivo"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _get_config_file() -> Path:
    return _get_config_dir() / "audio.json"


def get_selected_device() -> str:
    """Return the saved microphone device name, or empty string for default."""
    config_file = _get_config_file()
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                dev = data.get("input_device", "")
                if isinstance(dev, str):
                    return dev.strip()
        except Exception:
            pass

    # Fallback: check memory.config_manager if available
    try:
        from memory.config_manager import get_input_device
        return (get_input_device() or "").strip()
    except Exception:
        return ""


def save_selected_device(device_name: str) -> None:
    """Atomically save the chosen microphone device name."""
    clean_name = (device_name or "").strip()
    config_dir = _get_config_dir()
    config_file = _get_config_file()

    data: dict = {}
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}

    data["input_device"] = clean_name

    # Atomic write via temp file in the same directory
    payload = json.dumps(data, indent=2).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=str(config_dir), prefix="audio-cfg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, config_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Also sync to legacy config_manager if possible so both agree
    try:
        from memory.config_manager import save_input_device
        save_input_device(clean_name)
    except Exception:
        pass
