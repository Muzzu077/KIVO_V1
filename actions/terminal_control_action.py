"""
Terminal Control Action for KIVO (KIVO).

Enables full control over local terminal windows:
- Typing commands directly into visible terminals via Wayland (wtype) / X11 (xdotool)
- Running commands and reading the rendered output via OCR
- Executing background commands in isolated subprocesses
- Inspecting active shell CWD and busy/idle states
- Focusing and launching terminal windows
"""

from __future__ import annotations
import os
import time
from typing import Any
from core.terminal_control import get_terminal_controller, CommandResult

def terminal_control(parameters: dict, response=None, session_memory=None, player=None) -> str:
    tc = get_terminal_controller()
    action = str(parameters.get("action", "run")).lower().strip()
    command = parameters.get("command", "")
    directory = parameters.get("directory") or parameters.get("cwd") or parameters.get("path")
    window_id = parameters.get("window_id")

    if window_id is not None:
        try:
            window_id = int(window_id)
        except (ValueError, TypeError):
            window_id = None

    if action in ("type", "type_command"):
        return (
            "Arbitrary typing into terminal windows via legacy terminal_control is disabled for security. "
            "Use the KIVO Stage 10 'run_readonly_command' tool with explicit user approval."
        )

    elif action in ("run", "run_and_read", "execute_bg", "subprocess", "exec"):
        return (
            "Arbitrary shell execution via legacy terminal_control is disabled for security. "
            "Use the KIVO Stage 10 'run_readonly_command' tool with explicit user approval."
        )

    elif action in ("read", "read_output", "read_screen"):
        wait_sec = float(parameters.get("wait_seconds", 0.3))
        output = tc.capture_terminal_output(wait_seconds=wait_sec)
        if not output.strip():
            return "No text could be read from the terminal screen."
        return f"Terminal screen output:\n{output[:1500]}"

    elif action in ("status", "inspect"):
        state = tc.get_terminal_state()
        if not state:
            terms = tc.list_terminals()
            if not terms:
                return "No terminal windows currently open."
            state = terms[0]

        status_str = "Idle at prompt" if state.is_idle else f"Busy: running '{state.running_command}'"
        return (
            f"Terminal ({state.window.app_id}, PID {state.shell_pid}):\n"
            f"  Status: {status_str}\n"
            f"  Shell:  {state.shell_name}\n"
            f"  CWD:    {state.cwd or 'unknown'}"
        )

    elif action in ("list", "list_terminals"):
        return tc.describe_terminals()

    elif action in ("focus", "focus_terminal"):
        ok = tc.focus_terminal(window_id=window_id)
        return "Focused terminal window." if ok else "Could not focus terminal (no terminal found)."

    elif action in ("open", "new", "launch"):
        # Launch empty terminal optionally in a directory; never execute arbitrary commands on launch
        res = tc.open_terminal(directory=directory)
        return f"Terminal open request: {res}"

    else:
        return (
            f"Unknown terminal action '{action}'. "
            f"Valid actions: read_output, status, list, focus, open."
        )


TOOL = {
    "name": "terminal_control",
    "description": (
        "Inspect and focus local terminal windows on Arch Linux. Use this to check active shell "
        "working directory, read visible terminal screen output, list open terminals, or focus a terminal window. "
        "Arbitrary shell execution is disabled; use 'run_readonly_command' for approved terminal inspection."
    ),
    "handler": terminal_control,
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "read_output | status | list | focus | open"
            },
            "directory": {
                "type": "STRING",
                "description": "Optional working directory path (e.g. '~/Projects/KIVO')"
            },
            "wait_seconds": {
                "type": "NUMBER",
                "description": "Seconds to wait before reading terminal output via OCR (default 1.5)"
            },
            "window_id": {
                "type": "INTEGER",
                "description": "Optional target terminal window ID (from 'list' action)"
            }
        },
        "required": ["action"]
    }
}
