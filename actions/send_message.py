"""
actions/send_message.py — Universal Desktop & Web Chat Controller for JARVIS.

Supports sending messages across:
  - WhatsApp (WhatsApp Web via web.whatsapp.com or native app)
  - Telegram (Telegram Web or telegram-desktop)
  - Discord (Discord Web or native app)
  - Slack (Slack Web or native app)
  - Signal (Signal desktop)
  - Instagram (Web Direct)
  - Messenger (Web Messenger)
  - Active Chat Window (directly sends into whatever window is currently focused)

Features compositor-aware window focusing (niri, Hyprland, X11, Mac, Windows)
and Wayland-native input (wtype, wl-copy) with pyautogui fallbacks.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

_OS = platform.system()  # "Linux" | "Darwin" | "Windows"


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _is_wayland() -> bool:
    return _OS == "Linux" and bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland")


# ── Native Input Helpers (Wayland-first on Linux) ──────────────────────────

def _press_key(key: str) -> None:
    """Press a single key (e.g. Return, Tab, Escape)."""
    key_clean = "Return" if key.lower() in ("enter", "return") else key

    if _is_wayland() and shutil.which("wtype"):
        try:
            subprocess.run(["wtype", "-k", key_clean], timeout=3, capture_output=True)
            return
        except Exception:
            pass

    if _PYAUTOGUI:
        pyautogui.press(key.lower())


def _hotkey(*keys) -> None:
    """Press a key combination (e.g. ctrl, v or ctrl, alt, slash)."""
    if _is_wayland() and shutil.which("wtype"):
        try:
            cmd = ["wtype"]
            modifiers = [k.lower() for k in keys[:-1]]
            main_key = keys[-1].lower()
            if main_key in ("enter", "return"):
                main_key = "Return"
            elif main_key == "slash" or main_key == "/":
                main_key = "slash"

            for m in modifiers:
                cmd.extend(["-M", m])
            cmd.extend(["-k", main_key])
            for m in reversed(modifiers):
                cmd.extend(["-m", m])

            subprocess.run(cmd, timeout=3, capture_output=True)
            return
        except Exception:
            pass

    if _PYAUTOGUI:
        pyautogui.hotkey(*keys)


def _paste_text(text: str) -> None:
    """Paste text into focused window safely."""
    # 1. Wayland clipboard copy + paste
    if _is_wayland() and shutil.which("wl-copy") and shutil.which("wtype"):
        try:
            subprocess.run(["wl-copy", "--", text], timeout=3, capture_output=True)
            time.sleep(0.1)
            subprocess.run(["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"], timeout=3, capture_output=True)
            time.sleep(0.1)
            return
        except Exception:
            pass

    # 2. Pyperclip + PyAutoGUI hotkey
    paste_hotkey = ("command", "v") if _OS == "Darwin" else ("ctrl", "v")
    if _PYPERCLIP and _PYAUTOGUI:
        try:
            pyperclip.copy(text)
            time.sleep(0.1)
            pyautogui.hotkey(*paste_hotkey)
            time.sleep(0.1)
            return
        except Exception:
            pass

    # 3. Direct typing via wtype
    if _is_wayland() and shutil.which("wtype"):
        try:
            subprocess.run(["wtype", "--", text], timeout=4, capture_output=True)
            return
        except Exception:
            pass

    if _PYAUTOGUI:
        pyautogui.write(text, interval=0.02)


def _clear_and_paste(text: str) -> None:
    """Select all, delete, and paste text."""
    select_all = ("command", "a") if _OS == "Darwin" else ("ctrl", "a")
    _hotkey(*select_all)
    time.sleep(0.1)
    _press_key("Delete")
    time.sleep(0.1)
    _paste_text(text)


# ── Window & App Discovery ─────────────────────────────────────────────────

def _focus_matching_window(search_terms: list[str]) -> bool:
    """Inspect open windows via desktop context and bring matching window to front."""
    try:
        from core.desktop_context import get_desktop_context
        ctx = get_desktop_context()
        snap = ctx.refresh()

        for win in snap.windows:
            title_lower = win.title.lower()
            app_lower = win.app_id.lower()
            if any(t.lower() in title_lower or t.lower() in app_lower for t in search_terms):
                ctx.focus_window(win.window_id)
                time.sleep(0.3)
                return True
    except Exception as e:
        print(f"[SendMessage] Focus lookup error: {e}")
    return False


def _open_app_or_web(app_names: list[str], fallback_url: str) -> bool:
    """Try to focus or launch a native desktop app; fallback to opening web URL."""
    # 1. Check if already open
    if _focus_matching_window(app_names):
        print(f"[SendMessage] Found open chat window for {app_names[0]}")
        return True

    # 2. Try launching native app
    for app in app_names:
        if shutil.which(app.lower()):
            try:
                subprocess.Popen([app.lower()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(2.0)
                _focus_matching_window(app_names)
                return True
            except Exception:
                pass

        if _OS == "Linux" and shutil.which("gtk-launch"):
            try:
                r = subprocess.run(["gtk-launch", app.lower()], capture_output=True, timeout=3)
                if r.returncode == 0:
                    time.sleep(2.0)
                    _focus_matching_window(app_names)
                    return True
            except Exception:
                pass

        if _OS == "Darwin":
            try:
                subprocess.run(["open", "-a", app], capture_output=True, timeout=5)
                time.sleep(2.0)
                return True
            except Exception:
                pass

    # 3. Fallback: Open web URL in default browser
    print(f"[SendMessage] Launching Web interface: {fallback_url}")
    webbrowser.open(fallback_url)
    time.sleep(3.5)
    _focus_matching_window(["chrome", "firefox", "chromium", "brave", "edge", "browser"] + app_names)
    return True


# ── Platform Handlers ──────────────────────────────────────────────────────

def _is_phone_number(receiver: str) -> bool:
    """Check if receiver string looks like an international phone number."""
    cleaned = re.sub(r"[\s\-\(\)\+]", "", receiver)
    return cleaned.isdigit() and len(cleaned) >= 7


def _send_whatsapp(receiver: str, message: str) -> str:
    """Send via WhatsApp Web or native app."""
    # Direct phone number URL scheme: opens the chat with the draft prefilled
    if _is_phone_number(receiver):
        clean_num = re.sub(r"[^\d]", "", receiver)
        encoded_msg = urllib.parse.quote(message)
        direct_url = f"https://web.whatsapp.com/send?phone={clean_num}&text={encoded_msg}"
        webbrowser.open(direct_url)
        time.sleep(4.0)
        _focus_matching_window(["whatsapp", "chrome", "firefox", "chromium", "brave"])
        time.sleep(1.0)
        _press_key("Return")
        return f"Opened WhatsApp chat for +{clean_num} and sent message."

    # Contact name search
    opened = _open_app_or_web(["whatsapp", "zapzap"], "https://web.whatsapp.com")
    if not opened:
        return "Could not open WhatsApp desktop or web."

    time.sleep(1.0)
    # Shortcut to search in WhatsApp Web: Ctrl + Alt + / or Ctrl + F
    select_key = "command" if _OS == "Darwin" else "ctrl"
    _hotkey(select_key, "alt", "slash")
    time.sleep(0.4)
    # Also trigger Ctrl+F just in case
    _hotkey(select_key, "f")
    time.sleep(0.4)

    # Search for recipient
    _clear_and_paste(receiver)
    time.sleep(1.2)
    _press_key("Return")
    time.sleep(0.8)

    # Type and send message
    _paste_text(message)
    time.sleep(0.2)
    _press_key("Return")
    time.sleep(0.3)
    return f"Message sent to {receiver} via WhatsApp."


def _send_telegram(receiver: str, message: str) -> str:
    """Send via Telegram Desktop or Telegram Web."""
    opened = _open_app_or_web(["telegram-desktop", "telegram", "kotatogram"], "https://web.telegram.org/k/")
    if not opened:
        return "Could not open Telegram."

    time.sleep(1.0)
    select_key = "command" if _OS == "Darwin" else "ctrl"
    # Quick search in Telegram: Ctrl + K or Ctrl + F
    _hotkey(select_key, "k")
    time.sleep(0.3)
    _clear_and_paste(receiver)
    time.sleep(1.0)
    _press_key("Return")
    time.sleep(0.7)

    _paste_text(message)
    time.sleep(0.2)
    _press_key("Return")
    return f"Message sent to {receiver} via Telegram."


def _send_discord(receiver: str, message: str) -> str:
    """Send via Discord Desktop or Discord Web."""
    opened = _open_app_or_web(["discord", "vesktop", "webcord"], "https://discord.com/app")
    if not opened:
        return "Could not open Discord."

    time.sleep(1.2)
    select_key = "command" if _OS == "Darwin" else "ctrl"
    # Quick Switcher in Discord: Ctrl + K
    _hotkey(select_key, "k")
    time.sleep(0.4)
    _clear_and_paste(receiver)
    time.sleep(0.8)
    _press_key("Return")
    time.sleep(0.8)

    _paste_text(message)
    time.sleep(0.2)
    _press_key("Return")
    return f"Message sent to {receiver} via Discord."


def _send_slack(receiver: str, message: str) -> str:
    """Send via Slack Desktop or Slack Web."""
    opened = _open_app_or_web(["slack"], "https://app.slack.com/client")
    if not opened:
        return "Could not open Slack."

    time.sleep(1.2)
    select_key = "command" if _OS == "Darwin" else "ctrl"
    # Quick switcher in Slack: Ctrl + K
    _hotkey(select_key, "k")
    time.sleep(0.4)
    _clear_and_paste(receiver)
    time.sleep(0.8)
    _press_key("Return")
    time.sleep(0.8)

    _paste_text(message)
    time.sleep(0.2)
    _press_key("Return")
    return f"Message sent to {receiver} via Slack."


def _send_signal(receiver: str, message: str) -> str:
    """Send via Signal Desktop."""
    opened = _open_app_or_web(["signal-desktop", "signal"], "https://signal.org")
    if not opened:
        return "Could not open Signal."

    time.sleep(1.0)
    select_key = "command" if _OS == "Darwin" else "ctrl"
    _hotkey(select_key, "f")
    time.sleep(0.4)
    _clear_and_paste(receiver)
    time.sleep(1.0)
    _press_key("Return")
    time.sleep(0.7)

    _paste_text(message)
    time.sleep(0.2)
    _press_key("Return")
    return f"Message sent to {receiver} via Signal."


def _send_instagram(receiver: str, message: str) -> str:
    """Send via Instagram Direct Web."""
    webbrowser.open("https://www.instagram.com/direct/new/")
    time.sleep(4.0)
    _focus_matching_window(["instagram", "chrome", "firefox", "chromium", "brave"])

    _paste_text(receiver)
    time.sleep(1.5)
    _press_key("Down")
    time.sleep(0.3)
    _press_key("Return")
    time.sleep(0.4)

    for _ in range(4):
        _press_key("Tab")
        time.sleep(0.1)
    _press_key("Return")
    time.sleep(1.8)

    _paste_text(message)
    time.sleep(0.2)
    _press_key("Return")
    return f"Message sent to {receiver} via Instagram."


def _send_messenger(receiver: str, message: str) -> str:
    """Send via Messenger Web."""
    webbrowser.open("https://www.messenger.com/")
    time.sleep(4.0)
    _focus_matching_window(["messenger", "chrome", "firefox", "chromium", "brave"])

    select_key = "command" if _OS == "Darwin" else "ctrl"
    _hotkey(select_key, "k")
    time.sleep(0.5)
    _clear_and_paste(receiver)
    time.sleep(1.0)
    _press_key("Down")
    time.sleep(0.3)
    _press_key("Return")
    time.sleep(0.8)

    _paste_text(message)
    time.sleep(0.2)
    _press_key("Return")
    return f"Message sent to {receiver} via Messenger."


def _send_active_window(receiver_or_msg: str = "", message: str = "") -> str:
    """Directly send message into the currently focused chat or text field."""
    text_to_send = message if message else receiver_or_msg
    _paste_text(text_to_send)
    time.sleep(0.2)
    _press_key("Return")
    return f"Typed and sent message into active chat window: '{text_to_send[:50]}'"


_PLATFORM_MAP = [
    ({"whatsapp", "wp", "wapp", "whatsapp_web"}, "_send_whatsapp"),
    ({"telegram", "tg", "telegram_web"},        "_send_telegram"),
    ({"discord", "discord_web"},                "_send_discord"),
    ({"slack", "slack_web"},                    "_send_slack"),
    ({"signal"},                                "_send_signal"),
    ({"instagram", "ig", "insta"},              "_send_instagram"),
    ({"messenger", "facebook", "fb"},           "_send_messenger"),
    ({"active", "active_window", "current", "here"}, "_send_active_window"),
]


def _resolve_platform(platform_str: str):
    key = platform_str.lower().strip()
    for keywords, func_name in _PLATFORM_MAP:
        if any(k in key for k in keywords):
            return globals().get(func_name)
    # Generic fallback
    if "web" in key:
        return globals().get("_send_whatsapp")
    return globals().get("_send_active_window")


# ── Action Entrypoint ──────────────────────────────────────────────────────

def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    receiver = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    plat = params.get("platform", "whatsapp").strip()

    if not message_text:
        return "Please specify the message content."

    # If active window was requested, receiver can be empty
    if plat.lower() not in ("active", "active_window", "current", "here") and not receiver:
        return "Please specify a recipient contact name or phone number."

    preview = message_text[:50] + ("…" if len(message_text) > 50 else "")
    print(f"[SendMessage] 📨 {plat} → {receiver or 'Active Window'}: {preview}")
    if player:
        player.write_log(f"[msg] {plat} → {receiver or 'Active'}")

    try:
        handler = _resolve_platform(plat)
        result = handler(receiver, message_text)
    except Exception as e:
        result = f"Could not send message: {e}"

    lowered = result.lower()
    ok = "sent" in lowered and "not sent" not in lowered
    print(f"[SendMessage] {'✅' if ok else '❌'} {result}")
    if player:
        player.write_log(f"[msg] {result}")

    return result


# ── Tool Declaration (auto-discovered by core/action_loader.py) ────────────

TOOL = {
    "name": "send_message",
    "description": (
        "Sends a message through desktop or web chat platforms: WhatsApp (WhatsApp Web / native), "
        "Telegram (Web / native), Discord (Web / native), Slack, Signal, Instagram, Messenger, "
        "or directly into the active chat window on screen. "
        "Specify the recipient (name or phone number) and the exact message text in the user's language. "
        "Use platform='whatsapp', 'telegram', 'discord', 'slack', or 'active_window'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "receiver": {
                "type": "STRING",
                "description": "Recipient contact name, group name, or international phone number (+1234567890). Leave blank if platform='active_window'."
            },
            "message_text": {
                "type": "STRING",
                "description": "The exact message content to type and send."
            },
            "platform": {
                "type": "STRING",
                "description": "Messaging platform: 'whatsapp', 'whatsapp_web', 'telegram', 'discord', 'slack', 'instagram', 'signal', 'messenger', or 'active_window'."
            }
        },
        "required": [
            "message_text"
        ]
    },
    "handler": send_message,
}
