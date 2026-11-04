"""Persistent configuration and credential resolution for KIVO conversation.

Privacy Guarantee:
    - Cloud AI processing is strictly disabled by default (cloud_enabled = False).
    - Enabling cloud AI requires explicit user consent.
    - API keys are resolved securely from memory/config_manager or environment
      variables and are NEVER exposed over IPC or logged.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
DEFAULT_MAX_MESSAGES = 20
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 1024


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
    return _get_config_dir() / "conversation.json"


def load_conversation_config() -> dict:
    """Load conversation configuration, returning safe defaults.

    Cloud processing is disabled by default.
    """
    cfg_file = _get_config_file()
    defaults = {
        "cloud_enabled": False,  # Strict default
        "provider": DEFAULT_PROVIDER,
        "model": DEFAULT_MODEL,
        "max_messages": DEFAULT_MAX_MESSAGES,
        "temperature": DEFAULT_TEMPERATURE,
        "max_tokens": DEFAULT_MAX_TOKENS,
    }
    if not cfg_file.is_file():
        return defaults

    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return defaults
        return {
            "cloud_enabled": bool(data.get("cloud_enabled", False)),
            "provider": str(data.get("provider", DEFAULT_PROVIDER)).strip().lower(),
            "model": str(data.get("model", DEFAULT_MODEL)).strip(),
            "max_messages": max(2, min(50, int(data.get("max_messages", DEFAULT_MAX_MESSAGES)))),
            "temperature": max(0.0, min(2.0, float(data.get("temperature", DEFAULT_TEMPERATURE)))),
            "max_tokens": max(128, min(4096, int(data.get("max_tokens", DEFAULT_MAX_TOKENS)))),
        }
    except Exception:
        return defaults


def save_conversation_config(
    cloud_enabled: Optional[bool] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    max_messages: Optional[int] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> dict:
    """Atomically save conversation configuration."""
    current = load_conversation_config()

    if cloud_enabled is not None:
        current["cloud_enabled"] = bool(cloud_enabled)
    if provider is not None and provider.strip():
        current["provider"] = provider.strip().lower()
    if model is not None and model.strip():
        current["model"] = model.strip()
    if max_messages is not None:
        current["max_messages"] = max(2, min(50, int(max_messages)))
    if temperature is not None:
        current["temperature"] = max(0.0, min(2.0, float(temperature)))
    if max_tokens is not None:
        current["max_tokens"] = max(128, min(4096, int(max_tokens)))

    cfg_dir = _get_config_dir()
    cfg_file = _get_config_file()
    payload = json.dumps(current, indent=2).encode("utf-8")

    fd, tmp_path = tempfile.mkstemp(dir=str(cfg_dir), prefix="conv-cfg-", suffix=".tmp")
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


def get_openrouter_api_key() -> str:
    """Resolve OpenRouter API key without printing or logging its value.

    Order of precedence:
      1. Environment variable OPENROUTER_API_KEY
      2. Stored key in config/api_keys.json (via memory.config_manager)
    """
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        return env_key

    try:
        from memory.config_manager import load_api_keys
        keys = load_api_keys()
        # Direct key
        key = keys.get("openrouter_api_key", "")
        if isinstance(key, str) and key.strip():
            return key.strip()

        # In providers creds block
        providers = keys.get("providers")
        if isinstance(providers, dict):
            creds = providers.get("creds", {})
            if isinstance(creds, dict):
                or_cfg = creds.get("openrouter", {})
                if isinstance(or_cfg, dict):
                    or_key = or_cfg.get("api_key", "")
                    if isinstance(or_key, str) and or_key.strip():
                        return or_key.strip()
    except Exception:
        pass

    return ""
