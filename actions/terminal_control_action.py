"""
Terminal Control Action for JARVIS (Mark-LV).

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
        if not command:
            return "Please provide a 'command' to type into the terminal."
        press_enter = bool(parameters.get("press_enter", True))
        
        # If no terminal open, open one first
        terms = tc.list_terminals()
        if not terms:
            tc.open_terminal(directory=directory)
            time.sleep(0.5)

        res = tc.type_command_in_terminal(command=command, press_enter=press_enter)
        if res.success:
            return f"Typed command into terminal: '{command}'"
        return f"Failed to type command: {res.error}"

    elif action in ("run", "run_and_read"):
        if not command:
            return "Please provide a 'command' to run."

        # If background execution requested
        if parameters.get("background", False):
            sub_res = tc.run_in_subprocess(command=command, cwd=directory)
            if sub_res.success:
                return f"Subprocess output:\n{sub_res.output}"
            return f"Subprocess failed:\n{sub_res.error or sub_res.output}"

        # Interactive terminal typing + OCR capture
        terms = tc.list_terminals()
        if not terms:
            tc.open_terminal(directory=directory)
            time.sleep(0.6)

        wait_sec = float(parameters.get("wait_seconds", 1.5))
        type_res = tc.type_command_in_terminal(command=command, press_enter=True)
        if not type_res.success:
            return f"Could not type into terminal: {type_res.error}"

        time.sleep(wait_sec)
        screen_output = tc.capture_terminal_output(wait_seconds=0.2)
        return (
            f"Command sent to terminal: '{command}'.\n"
            f"Terminal screen content:\n{screen_output[:1200]}"
        )

    elif action in ("execute_bg", "subprocess", "exec"):
        if not command:
            return "Please provide a 'command' to execute in background."
        res = tc.run_in_subprocess(command=command, cwd=directory)
        if res.success:
            return f"Command succeeded:\n{res.output}"
        return f"Command error:\n{res.error or res.output}"

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
        res = tc.open_terminal(directory=directory, command=command)
        return f"Terminal open request: {res}"

    else:
        return (
            f"Unknown terminal action '{action}'. "
            f"Valid actions: type, run, execute_bg, read_output, status, list, focus, open."
        )


TOOL = {
    "name": "terminal_control",
    "description": (
        "Full control over local terminal windows on Arch Linux. Use this whenever the user asks "
        "to run terminal/shell/bash commands, type in the terminal, check what directory the terminal is in, "
        "read terminal output or screen text, open a terminal, or focus a terminal window."
    ),
    "handler": terminal_control,
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "type | run | execute_bg | read_output | status | list | focus | open"
            },
            "command": {
                "type": "STRING",
                "description": "The shell command to type or execute (e.g. 'git status', 'pip install send2trash', 'ls -la')"
            },
            "directory": {
                "type": "STRING",
                "description": "Optional working directory path (e.g. '~/Projects/Mark-LV')"
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
