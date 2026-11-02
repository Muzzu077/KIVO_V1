"""
core/platform_support.py — what this particular machine can actually let the
assistant do, and what it cannot.

WHY THIS EXISTS
    "It doesn't work properly on Linux" and "it's bad on macOS" were reported
    for a long time without anybody being able to say WHICH part failed. The
    answer turns out to be the same in both cases, and it is not a bug in any
    one file: on some systems the operating system refuses to let any program
    drive the keyboard, the mouse or the screen, and everything built on top of
    that then fails silently.

    LINUX / WAYLAND
        Wayland is the default session on Ubuntu 22.04 and later, on Fedora,
        and on current GNOME and KDE. It has no equivalent of X11's "any client
        may synthesise input anywhere" — that was the security hole it was
        designed to close. So `pyautogui` cannot type or click, `mss` cannot
        capture the screen, and `xdotool` and `wmctrl` do nothing. None of them
        raises: they return success and have no effect, which is the worst
        possible failure and precisely why this reads as "the assistant is
        broken" rather than "the platform does not permit this".

        Wayland is not uniformly impossible, though. Each compositor offers a
        sanctioned route, and this module probes for those instead of giving
        up: `grim` captures via wlr-screencopy, niri/Hyprland/sway expose IPC
        for window focus, and `wtype`/`ydotool` inject input through the
        compositor or uinput. What remains impossible is injection into a
        native app that refuses it, and that is what the notes report.

    macOS / PERMISSIONS
        Driving the keyboard needs Accessibility, and capturing the screen
        needs Screen Recording. Both are off until the user grants them in
        System Settings, and until then the same APIs return success and do
        nothing.

WHAT THIS MODULE DOES ABOUT IT
    It does not fix any of that — none of it is fixable from inside the app.
    It makes it VISIBLE: the limits are reported to the user in the activity
    log, and, more importantly, to the MODEL through `_describe_limits`, so the
    assistant says "I can't drive the screen in this session, because Wayland
    does not allow it" instead of cheerfully reporting a click that never
    happened.

    That is the same rule the rest of this release follows: never claim an
    outcome that was not observed. A platform that forbids the action is just
    another thing that was not observed.
"""
from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field


@dataclass
class Support:
    """What is possible here. Every field is a fact about THIS machine."""

    os_name: str
    can_control_input: bool = True      # type, click, hotkeys
    can_capture_screen: bool = True     # screenshots, vision
    can_focus_windows: bool = True      # raise another app's window
    notes: list[str] = field(default_factory=list)

    @property
    def fully_capable(self) -> bool:
        return (self.can_control_input and self.can_capture_screen
                and self.can_focus_windows)


def _linux_session() -> str:
    """'wayland', 'x11', or '' when there is no graphical session at all."""
    session = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if session in ("wayland", "x11"):
        return session
    # XDG_SESSION_TYPE is not always set — a compositor's own variable is the
    # reliable second opinion, and DISPLAY is the last one.
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return ""


def detect() -> Support:
    """Work out what this machine permits. Never raises, never blocks.

    Deliberately cheap — environment variables and `shutil.which` — because it
    runs at startup and because the expensive way to find out is to try the
    action and watch it silently do nothing, which is the problem.
    """
    system = platform.system()

    if system == "Windows":
        return Support(os_name="Windows")

    if system == "Darwin":
        support = Support(os_name="macOS")
        # There is no reliable way to READ the Accessibility permission without
        # a native extension, so this cannot be a hard answer — it is a warning
        # attached to the thing people actually get wrong.
        support.notes.append(
            "macOS asks for two permissions before an assistant can drive the "
            "computer: Accessibility (to type and click) and Screen Recording "
            "(to see the screen), both under System Settings > Privacy & "
            "Security. Until they are granted, those actions appear to succeed "
            "and do nothing."
        )
        return support

    # ── Linux ────────────────────────────────────────────────────────────────
    session = _linux_session()
    support = Support(os_name="Linux")

    if not session:
        support.can_control_input = False
        support.can_capture_screen = False
        support.can_focus_windows = False
        support.notes.append(
            "No graphical session was found, so nothing on this machine can be "
            "typed, clicked, captured or focused. Voice, memory, files and the "
            "web still work."
        )
        return support

    if session == "wayland":
        # Wayland forbids the X11 free-for-all, but each compositor ships its
        # own sanctioned route. Report what is actually installed rather than
        # declaring the whole session incapable: on this machine grim captures,
        # niri focuses windows, and wtype types.
        has_grim = bool(shutil.which("grim"))
        has_typer = any(shutil.which(t) for t in ("wtype", "ydotool", "dotool"))
        has_compositor_ipc = any(
            shutil.which(t) for t in ("niri", "hyprctl", "swaymsg")
        )

        support.can_capture_screen = has_grim
        support.can_control_input = has_typer
        support.can_focus_windows = has_compositor_ipc

        if not has_grim:
            support.notes.append(
                "grim is not installed, so the screen cannot be captured on this "
                "Wayland session. Install grim (pacman -S grim) to enable vision."
            )
        if not has_typer:
            support.notes.append(
                "No Wayland input tool was found, so typing and key presses are "
                "unavailable. Install wtype (pacman -S wtype) — ydotool also works "
                "if its daemon is running."
            )
        if not has_compositor_ipc:
            support.notes.append(
                "No supported compositor IPC was found (niri, Hyprland or sway), "
                "so an already-running window cannot be raised or inspected."
            )
        if support.fully_capable:
            support.notes.append(
                "Wayland is handled through the compositor's own tools: grim for "
                "capture, the compositor IPC for windows, and wtype/ydotool for "
                "input. Some native apps may still refuse synthetic input."
            )
        return support

    # X11: everything is possible, but two small tools carry window focus.
    if not (shutil.which("xdotool") or shutil.which("wmctrl")):
        support.can_focus_windows = False
        support.notes.append(
            "Neither xdotool nor wmctrl is installed, so an application that "
            "is already running cannot be brought to the front — it will be "
            "launched again instead. Install either one to fix that."
        )
    return support


def describe_for_model(support: Support) -> list[str]:
    """The limits, phrased for `_describe_limits` in main.py.

    Written as facts about the machine rather than as refusals, because a model
    told "you cannot do X here, and this is why" explains it to the user; a
    model told "do not do X" invents a reason.
    """
    lines: list[str] = []
    if not support.can_control_input:
        lines.append(
            "- You CANNOT type, click, press keys or move the mouse in this "
            "session: the input tool this desktop needs is not installed. Do "
            "not attempt it and do not report having done it — say plainly "
            "which tool is missing.")
    if not support.can_capture_screen:
        lines.append(
            "- You CANNOT see the screen in this session because the capture "
            "tool is not installed. Say so rather than describing anything.")
    if not support.can_focus_windows and support.can_control_input:
        lines.append(
            "- You cannot bring an already-running application to the front "
            "here; opening it starts another window instead.")
    return lines


def describe_for_user(support: Support) -> list[str]:
    """One line per note, for the activity log at startup."""
    return [f"SYS: {note}" for note in support.notes]
