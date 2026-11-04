"""Persistent configuration for the KIVO STT engine.

Stores the user's STT preferences (engine, model, language, device) in an
XDG-compatible JSON configuration file using atomic writes.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

DEFAULT_ENGINE = "faster-whisper"
DEFAULT_MODEL = "base"
DEFAULT_LANGUAGE = "auto"
DEFAULT_DEVICE = "cpu"

VALID_ENGINES = ("faster-whisper", "whisper")
VALID_MODELS = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")
VALID_LANGUAGES = ("auto", "en", "hi", "te")


def _get_config_dir() -> Path:
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config and os.path.isabs(xdg_config):
        base = Path(xdg_config)
    else:
        base = Path.home() / ".config"
    cfg_dir = base / "kivo"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    return cfg_dir


def _get_config_file() -> Path:
    return _get_config_dir() / "stt.json"


def load_stt_config() -> dict:
    """Load STT configuration, returning safe defaults if missing or corrupted."""
    cfg_file = _get_config_file()
    defaults = {
        "engine": DEFAULT_ENGINE,
        "model": DEFAULT_MODEL,
        "language": DEFAULT_LANGUAGE,
        "device": DEFAULT_DEVICE,
    }
    if not cfg_file.is_file():
        return defaults

    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return defaults
        return {
            "engine": str(data.get("engine", DEFAULT_ENGINE)).strip(),
            "model": str(data.get("model", DEFAULT_MODEL)).strip(),
            "language": str(data.get("language", DEFAULT_LANGUAGE)).strip().lower() or "auto",
            "device": str(data.get("device", DEFAULT_DEVICE)).strip().lower(),
        }
    except Exception:
        return defaults


def save_stt_config(
    engine: str | None = None,
    model: str | None = None,
    language: str | None = None,
    device: str | None = None,
) -> dict:
    """Atomically save updated STT settings."""
    current = load_stt_config()
    if engine is not None and engine in VALID_ENGINES:
        current["engine"] = engine
    if model is not None and model in VALID_MODELS:
        current["model"] = model
    if language is not None:
        clean_lang = language.strip().lower()
        if clean_lang in ("", "auto") or len(clean_lang) == 2:
            current["language"] = clean_lang or "auto"
    if device is not None and device in ("cpu", "cuda"):
        current["device"] = device

    cfg_dir = _get_config_dir()
    cfg_file = _get_config_file()
    payload = json.dumps(current, indent=2).encode("utf-8")

    fd, tmp_path = tempfile.mkstemp(dir=str(cfg_dir), prefix="stt-cfg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, cfg_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return current
