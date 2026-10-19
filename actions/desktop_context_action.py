"""
actions/desktop_context_action.py — Desktop awareness and continuous screen monitoring tool.

Exposes desktop state, open windows, terminal context, and screen observer controls
to the Gemini Live model.
"""
from __future__ import annotations

from core.desktop_context import get_desktop_context
from core.screen_observer import get_screen_observer
from core.terminal_control import get_terminal_controller


def desktop_context_handler(parameters: dict, **kwargs) -> str:
    """
    Query desktop state, inspect open windows, check what is on screen,
    or control screen monitoring (pause/resume).
    """
    action = parameters.get("action", "summary").lower().strip()
    ctx = get_desktop_context()
    observer = get_screen_observer()

    if action in ("summary", "current", "state"):
        # Real-time combined desktop & screen context
        screen_sum = observer.get_context_summary()
        snap = ctx.snapshot()
        fw = snap.focused_window
        win_info = f"Focused Window: {fw.app_id} ('{fw.title}')" if fw else "No window currently focused."
        return f"{win_info}\n\nDesktop Overview:\n{screen_sum}"

    elif action in ("windows", "list_windows"):
        snap = ctx.refresh()
        if not snap.windows:
            return "No open application windows detected."
        lines = [f"Open Windows ({len(snap.windows)}):"]
        for w in snap.windows:
            f_marker = " [FOCUSED]" if w.is_focused else ""
            lines.append(f"  • ID {w.window_id}: {w.app_id} — \"{w.title}\"{f_marker}")
        return "\n".join(lines)

    elif action in ("terminals", "list_terminals"):
        tc = get_terminal_controller()
        return tc.describe_terminals()

    elif action in ("focus", "focus_window", "switch_to"):
        target = parameters.get("target", "").strip()
        if not target:
            return "Please provide a target window title, app name, or ID."
        snap = ctx.refresh()
        # Try match by ID
        target_win = None
        for w in snap.windows:
            if str(w.window_id) == target:
                target_win = w
                break
        # Try match by app_id or title
        if not target_win:
            for w in snap.windows:
                if target.lower() in w.app_id.lower() or target.lower() in w.title.lower():
                    target_win = w
                    break
        if not target_win:
            return f"Could not find an open window matching '{target}'."
        ok = ctx.focus_window(target_win.window_id)
        return f"Focused {target_win.app_id} ('{target_win.title}')" if ok else f"Failed to focus window {target_win.window_id}."

    elif action in ("screen_text", "ocr", "read_screen"):
        obs = observer.get_latest_observation()
        if not obs or not obs.ocr_text_preview.strip():
            obs = observer.capture_observation()
        if obs and obs.ocr_text_preview.strip():
            return f"Active Application: {obs.active_app} ('{obs.active_title}')\n\nRecognized On-Screen Text:\n{obs.ocr_text_preview[:3000]}"
        return "No text could be extracted from the current screen."

    elif action in ("type_text", "type"):
        text = parameters.get("text", "")
        press_enter = bool(parameters.get("press_enter", False))
        if not text:
            return "Please provide text to type."
        import time
        from core.terminal_control import _type_text, _press_key
        target = parameters.get("target", "").strip()
        if target:
            snap = ctx.refresh()
            for w in snap.windows:
                if target.lower() in w.app_id.lower() or target.lower() in w.title.lower() or str(w.window_id) == target:
                    ctx.focus_window(w.window_id)
                    time.sleep(0.15)
                    break
        _type_text(text, ctx.session_type)
        if press_enter:
            _press_key("Return", ctx.session_type)
        return f"Typed text into focused window: '{text}'"

    elif action in ("press_key", "key"):
        key = parameters.get("key", "Return")
        from core.terminal_control import _press_key
        _press_key(key, ctx.session_type)
        return f"Pressed key '{key}' on desktop."

    elif action == "pause_monitor":
        observer.pause()
        return "Background screen monitoring has been paused (privacy mode enabled)."

    elif action == "resume_monitor":
        observer.resume()
        return "Background screen monitoring has been resumed."

    elif action == "monitor_status":
        active = observer.is_active()
        obs = observer.get_latest_observation()
        if obs:
            return f"Screen monitoring: {'ACTIVE' if active else 'PAUSED'}. Last update: {obs.active_app} ('{obs.active_title}')."
        return f"Screen monitoring: {'ACTIVE' if active else 'PAUSED'}."

    return (
        f"Unknown action '{action}'. Valid actions: summary, screen_text, list_windows, "
        f"list_terminals, focus_window, type_text, press_key, pause_monitor, resume_monitor, monitor_status."
    )


TOOL = {
    "name": "desktop_context",
    "description": (
        "Provides real-time continuous desktop and screen awareness. "
        "Allows JARVIS to inspect what is on the screen (summary or OCR text), list open windows, "
        "identify terminal directories, switch/focus windows, type text or press keys into active apps, "
        "or control background screen monitoring (pause/resume for privacy)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "The action to perform: "
                    "'summary' (what is on screen and focused right now), "
                    "'screen_text' (read full recognized text on current screen via OCR), "
                    "'list_windows' (all open application windows), "
                    "'list_terminals' (all open terminal shells and their directories), "
                    "'focus_window' (switch focus to a specific window), "
                    "'type_text' (type text into the active or target window), "
                    "'press_key' (press a key like Return, Escape, Tab), "
                    "'pause_monitor' (pause background screen capture for privacy), "
                    "'resume_monitor' (resume background screen capture), "
                    "'monitor_status' (check monitoring state)."
                ),
            },
            "target": {
                "type": "STRING",
                "description": "App name, window title fragment, or ID to focus when action='focus_window' or 'type_text'."
            },
            "text": {
                "type": "STRING",
                "description": "Text to type when action='type_text'."
            },
            "key": {
                "type": "STRING",
                "description": "Key to press when action='press_key' (e.g. Return, Escape, Tab, BackSpace)."
            },
            "press_enter": {
                "type": "BOOLEAN",
                "description": "Whether to press Enter after typing when action='type_text'."
            }
        },
        "required": ["action"]
    },
    "handler": desktop_context_handler,
}
