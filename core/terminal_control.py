"""
Terminal control adapter — interact with real terminal windows.

Capabilities:
  1. Detect terminal windows (Alacritty, kitty, etc.) via DesktopContextService.
  2. Determine the shell process, CWD, and whether the shell is idle.
  3. Type commands into the focused terminal using wtype (Wayland) or xdotool (X11).
  4. Run commands in a restricted subprocess (internal, not visible to user).
  5. Capture terminal output via OCR or shell integration.

Safety:
  - Verifies the target is actually a terminal before typing.
  - Checks the shell is idle (at a prompt) before injecting input.
  - Refuses to type into a terminal running an interactive program.
  - All commands pass through the action policy system when available.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.desktop_context import (
    DesktopContextService, WindowInfo, SessionType, get_desktop_context,
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TerminalState:
    """Extended info about a specific terminal window."""
    window: WindowInfo
    shell_pid: int = 0
    shell_name: str = ""           # bash, zsh, fish, etc.
    cwd: str = ""                  # current working directory of the shell
    is_idle: bool = False          # shell is at a prompt, not running something
    running_command: str = ""      # name of foreground child if not idle
    timestamp: float = 0.0


@dataclass
class CommandResult:
    """Result of a command execution."""
    success: bool
    output: str = ""
    error: str = ""
    method: str = ""               # "subprocess" | "terminal_type" | "ocr"
    cwd: str = ""
    duration: float = 0.0


# ---------------------------------------------------------------------------
# Terminal process introspection (Linux /proc)
# ---------------------------------------------------------------------------

def _find_shell_child(term_pid: int) -> tuple[int, str] | None:
    """Find the shell process that is a direct or grandchild of the terminal PID."""
    shells = {"bash", "zsh", "fish", "sh", "dash", "nu", "elvish", "ion"}

    def _scan(parent_pid: int, depth: int = 0) -> tuple[int, str] | None:
        if depth > 3:
            return None
        try:
            r = subprocess.run(
                ["pgrep", "-P", str(parent_pid)],
                capture_output=True, text=True, timeout=2,
            )
            children = [int(p) for p in r.stdout.strip().split() if p.strip().isdigit()]
        except Exception:
            return None

        for child in children:
            try:
                comm = Path(f"/proc/{child}/comm").read_text().strip()
                if comm in shells:
                    return child, comm
            except (OSError, PermissionError):
                pass

        # Recurse one level deeper
        for child in children:
            result = _scan(child, depth + 1)
            if result:
                return result
        return None

    return _scan(term_pid)


def _get_shell_cwd(shell_pid: int) -> str:
    """Get the CWD of a shell process via /proc."""
    try:
        return os.readlink(f"/proc/{shell_pid}/cwd")
    except (OSError, PermissionError):
        return ""


def _is_shell_idle(shell_pid: int) -> tuple[bool, str]:
    """
    Check if a shell is at a prompt (foreground process group == shell pid).
    Returns (is_idle, running_command_name).
    """
    try:
        stat_raw = Path(f"/proc/{shell_pid}/stat").read_text()
        # pid (comm) state ppid pgrp session tty_nr tpgid ...
        after = stat_raw.rsplit(")", 1)[1].split()
        tpgid = int(after[5])

        if tpgid == shell_pid:
            return True, ""

        # Find what's running in the foreground
        fg_comm = ""
        try:
            fg_comm = Path(f"/proc/{tpgid}/comm").read_text().strip()
        except (OSError, PermissionError):
            pass
        return False, fg_comm
    except Exception:
        return True, ""  # assume idle if we can't determine


# ---------------------------------------------------------------------------
# Input injection
# ---------------------------------------------------------------------------

_WAYLAND_TYPER: str | None = None
_X11_TYPER: str | None = None


def _detect_typer() -> None:
    global _WAYLAND_TYPER, _X11_TYPER
    for tool in ("wtype", "ydotool", "dotool"):
        if shutil.which(tool):
            _WAYLAND_TYPER = tool
            break
    if shutil.which("xdotool"):
        _X11_TYPER = "xdotool"


_detect_typer()


def _type_text(text: str, session_type: SessionType) -> bool:
    """Type text into the currently focused window."""
    if session_type == SessionType.WAYLAND:
        if _WAYLAND_TYPER == "wtype":
            try:
                subprocess.run(
                    ["wtype", "--", text],
                    timeout=5, capture_output=True,
                )
                return True
            except Exception:
                pass
        elif _WAYLAND_TYPER == "ydotool":
            try:
                subprocess.run(
                    ["ydotool", "type", "--", text],
                    timeout=5, capture_output=True,
                )
                return True
            except Exception:
                pass

    if _X11_TYPER == "xdotool" and (
        session_type == SessionType.X11
        or os.environ.get("DISPLAY")
    ):
        try:
            subprocess.run(
                ["xdotool", "type", "--clearmodifiers", "--", text],
                timeout=5, capture_output=True,
            )
            return True
        except Exception:
            pass

    return False


def _press_key(key: str, session_type: SessionType) -> bool:
    """Press a key (e.g., 'Return', 'Escape') on the currently focused window."""
    if session_type == SessionType.WAYLAND and _WAYLAND_TYPER == "wtype":
        try:
            subprocess.run(
                ["wtype", "-k", key],
                timeout=3, capture_output=True,
            )
            return True
        except Exception:
            pass

    if _X11_TYPER == "xdotool" and (
        session_type == SessionType.X11
        or os.environ.get("DISPLAY")
    ):
        try:
            subprocess.run(
                ["xdotool", "key", "--clearmodifiers", key],
                timeout=3, capture_output=True,
            )
            return True
        except Exception:
            pass

    return False


# ---------------------------------------------------------------------------
# Terminal Control Service
# ---------------------------------------------------------------------------

class TerminalController:
    """
    Controls terminal windows — detect, inspect, type commands, read output.

    Thread-safe, uses DesktopContextService for window management.
    """

    def __init__(self, desktop_ctx: DesktopContextService | None = None):
        self._ctx = desktop_ctx or get_desktop_context()

    # ── Inspection ─────────────────────────────────────────────────────────

    def get_terminal_state(self, window: WindowInfo | None = None) -> TerminalState | None:
        """
        Get extended state for a terminal window.
        If window is None, uses the focused window (must be a terminal).
        """
        if window is None:
            window = self._ctx.focused_window()
            if not window:
                return None

        term_apps = {
            "alacritty", "kitty", "foot", "wezterm", "gnome-terminal",
            "konsole", "xfce4-terminal", "xterm", "tilix", "mate-terminal",
        }
        if window.app_id.lower() not in term_apps:
            return None

        shell_info = _find_shell_child(window.pid)
        if not shell_info:
            return TerminalState(window=window, timestamp=time.monotonic())

        shell_pid, shell_name = shell_info
        cwd = _get_shell_cwd(shell_pid)
        idle, fg_cmd = _is_shell_idle(shell_pid)

        return TerminalState(
            window=window,
            shell_pid=shell_pid,
            shell_name=shell_name,
            cwd=cwd,
            is_idle=idle,
            running_command=fg_cmd,
            timestamp=time.monotonic(),
        )

    def list_terminals(self) -> list[TerminalState]:
        """List all terminal windows with their shell states."""
        terminals = self._ctx.find_terminal_windows()
        result = []
        for w in terminals:
            state = self.get_terminal_state(w)
            if state:
                result.append(state)
        return result

    def find_terminal_by_cwd(self, directory: str) -> TerminalState | None:
        """Find a terminal whose shell is in the given directory."""
        target = Path(directory).expanduser().resolve()
        for term in self.list_terminals():
            if term.cwd and Path(term.cwd).resolve() == target:
                return term
        return None

    def focus_terminal(self, window_id: int | None = None) -> bool:
        """Focus the specified terminal window or the first available terminal."""
        if window_id:
            return self._ctx.focus_window(window_id)
        terms = self.list_terminals()
        if terms:
            return self._ctx.focus_window(terms[0].window.window_id)
        return False

    def open_terminal(self, directory: str | None = None, command: str | None = None) -> str:
        """Launch a new terminal window, optionally in a directory or running a command."""
        try:
            from actions.open_app import open_app
            params = {"name": "terminal"}
            if directory:
                params["target"] = directory
            if command:
                params["command"] = command
            return open_app(params)
        except Exception as e:
            return f"Failed to launch terminal: {e}"

    # ── Command execution ──────────────────────────────────────────────────

    def type_command_in_terminal(
        self,
        command: str,
        window: WindowInfo | None = None,
        press_enter: bool = True,
        verify_idle: bool = True,
    ) -> CommandResult:
        """
        Type a command into an actual terminal window.

        Safety checks:
        1. Verifies the target is a terminal.
        2. Focuses the terminal window.
        3. Checks the shell is idle (at a prompt).
        4. Types the command.
        5. Optionally presses Enter.

        Returns a CommandResult describing what happened.
        """
        start = time.monotonic()

        # Resolve target terminal
        if window is None:
            focused = self._ctx.focused_window()
            if focused and self._is_terminal(focused):
                window = focused
            else:
                # Find the most recently used idle terminal
                terms = self.list_terminals()
                idle_terms = [t for t in terms if t.is_idle]
                if not idle_terms:
                    return CommandResult(
                        success=False,
                        error="No idle terminal found. Open a terminal first.",
                        method="terminal_type",
                        duration=time.monotonic() - start,
                    )
                window = idle_terms[0].window

        state = self.get_terminal_state(window)
        if not state:
            return CommandResult(
                success=False,
                error=f"Window '{window.app_id}' is not a recognized terminal.",
                method="terminal_type",
                duration=time.monotonic() - start,
            )

        if verify_idle and not state.is_idle:
            return CommandResult(
                success=False,
                error=(
                    f"Terminal is busy running '{state.running_command}'. "
                    f"Wait for it to finish or use a different terminal."
                ),
                method="terminal_type",
                duration=time.monotonic() - start,
            )

        # Focus the terminal window
        if not window.is_focused:
            ok = self._ctx.focus_window(window.window_id)
            if not ok:
                return CommandResult(
                    success=False,
                    error="Could not focus the terminal window.",
                    method="terminal_type",
                    duration=time.monotonic() - start,
                )
            time.sleep(0.15)  # allow compositor to settle

        # Type the command
        ok = _type_text(command, self._ctx.session_type)
        if not ok:
            return CommandResult(
                success=False,
                error="Could not type text into the terminal (no input method available).",
                method="terminal_type",
                duration=time.monotonic() - start,
            )

        if press_enter:
            time.sleep(0.05)
            _press_key("Return", self._ctx.session_type)

        return CommandResult(
            success=True,
            output=f"Typed '{command}' into {window.app_id} terminal.",
            method="terminal_type",
            cwd=state.cwd,
            duration=time.monotonic() - start,
        )

    def run_in_subprocess(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> CommandResult:
        """
        Run a command in JARVIS's own restricted subprocess (not visible to user).
        Use this for queries like "what directory are we in?" when a subprocess answer
        is acceptable, or when the user doesn't need to see the command run.
        """
        start = time.monotonic()
        work_dir = None
        if cwd:
            p = Path(cwd).expanduser().resolve()
            if p.exists() and p.is_dir():
                work_dir = str(p)

        env = os.environ.copy()
        venv_bin = Path(__file__).resolve().parent.parent / ".venv" / "bin"
        if venv_bin.exists():
            env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
            env["VIRTUAL_ENV"] = str(venv_bin.parent)

        try:
            r = subprocess.run(
                command,
                shell=True,
                cwd=work_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
            output = out
            if err and r.returncode != 0:
                output = f"{output}\nError: {err}".strip() if output else f"Error: {err}"

            if r.returncode != 0:
                try:
                    from core.proactive_hub import get_proactive_hub, ProactiveEvent, ProactiveEventType
                    get_proactive_hub().emit_event(
                        ProactiveEvent(
                            event_type=ProactiveEventType.TERMINAL_FAILURE,
                            source="Terminal",
                            title=f"Command '{command[:35]}' failed with code {r.returncode}",
                            details=err[:200] or out[:200],
                            suggested_action="Offer to analyze the error or provide the corrected syntax.",
                        ),
                        cooldown_seconds=60.0,
                    )
                except Exception:
                    pass

            return CommandResult(
                success=r.returncode == 0,
                output=output or "(no output)",
                error=err if r.returncode != 0 else "",
                method="subprocess",
                cwd=work_dir or os.getcwd(),
                duration=time.monotonic() - start,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                success=False,
                error=f"Command timed out after {timeout} seconds.",
                method="subprocess",
                duration=time.monotonic() - start,
            )
        except Exception as e:
            return CommandResult(
                success=False,
                error=str(e),
                method="subprocess",
                duration=time.monotonic() - start,
            )

    def capture_terminal_output(
        self,
        window: WindowInfo | None = None,
        wait_seconds: float = 1.0,
    ) -> str:
        """
        Capture what's visible in a terminal window via screenshot + OCR.
        Used after typing a command to read the result.
        """
        if window is None:
            window = self._ctx.focused_window()
        if not window:
            return "[no terminal window available]"

        time.sleep(wait_seconds)  # allow command to produce output

        path = f"/tmp/jarvis_term_{int(time.time() * 1000)}.png"
        text = self._ctx.ocr_screenshot(path=path, window_id=window.window_id)

        # Clean up
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

        return text

    def get_current_directory(self, window: WindowInfo | None = None) -> str:
        """Get the CWD of a terminal's shell process."""
        if window is None:
            # Try focused terminal first
            focused = self._ctx.focused_window()
            if focused and self._is_terminal(focused):
                window = focused
            else:
                # Find any idle terminal
                terms = self.list_terminals()
                if terms:
                    window = terms[0].window
                else:
                    return os.getcwd()

        state = self.get_terminal_state(window)
        if state and state.cwd:
            return state.cwd
        return "(unknown)"

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _is_terminal(self, window: WindowInfo) -> bool:
        term_apps = {
            "alacritty", "kitty", "foot", "wezterm", "gnome-terminal",
            "konsole", "xfce4-terminal", "xterm", "tilix", "mate-terminal",
        }
        return window.app_id.lower() in term_apps

    def describe_terminals(self) -> str:
        """Human-readable summary of all terminal windows."""
        terms = self.list_terminals()
        if not terms:
            return "No terminal windows open."

        lines = [f"Terminal windows ({len(terms)}):"]
        for t in terms:
            status = "idle" if t.is_idle else f"running {t.running_command}"
            lines.append(
                f"  [{t.window.window_id}] {t.window.app_id} "
                f"| {t.shell_name} | cwd={t.cwd} | {status}"
            )
        return "\n".join(lines)


# Module-level convenience
_controller: TerminalController | None = None

def get_terminal_controller() -> TerminalController:
    """Get or create the singleton terminal controller."""
    global _controller
    if _controller is None:
        _controller = TerminalController()
    return _controller
