"""
Desktop context service — maintains an up-to-date snapshot of the user's
desktop state: active window, workspaces, compositor, session type.

Supports:
  - niri (Wayland tiling compositor) via JSON IPC
  - Hyprland via hyprctl
  - sway via swaymsg
  - X11/generic via xdotool/wmctrl
  - Fallback: /proc, DBus, environment variables

All queries are non-blocking and cached with configurable staleness.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from threading import Lock
from typing import Optional

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class SessionType(Enum):
    WAYLAND = auto()
    X11 = auto()
    UNKNOWN = auto()


class Compositor(Enum):
    NIRI = auto()
    HYPRLAND = auto()
    SWAY = auto()
    GNOME = auto()
    KDE = auto()
    GENERIC_WAYLAND = auto()
    X11 = auto()
    UNKNOWN = auto()


@dataclass
class WindowInfo:
    """Snapshot of a single window."""
    window_id: int | str = 0
    title: str = ""
    app_id: str = ""             # Wayland app_id or X11 WM_CLASS
    pid: int = 0
    workspace_id: int | str = 0
    is_focused: bool = False
    is_floating: bool = False
    size: tuple[int, int] = (0, 0)       # (width, height)
    # Absolute screen position, or None when the compositor does not expose it.
    # niri's IPC gives tile size but no absolute origin, so guessing (0, 0) made
    # window-relative OCR read the top-left corner of the screen instead.
    position: tuple[int, int] | None = None
    timestamp: float = 0.0


@dataclass
class WorkspaceInfo:
    workspace_id: int | str = 0
    index: int = 0
    name: str = ""
    output: str = ""
    is_active: bool = False
    is_focused: bool = False
    active_window_id: int | str | None = None


@dataclass
class DesktopSnapshot:
    """Complete desktop state at a point in time."""
    timestamp: float = 0.0
    session_type: SessionType = SessionType.UNKNOWN
    compositor: Compositor = Compositor.UNKNOWN
    compositor_name: str = ""
    desktop_env: str = ""

    focused_window: Optional[WindowInfo] = None
    windows: list[WindowInfo] = field(default_factory=list)
    workspaces: list[WorkspaceInfo] = field(default_factory=list)

    active_workspace_id: int | str | None = None

    # Derived convenience
    active_app: str = ""         # app_id of focused window
    active_title: str = ""       # title of focused window
    active_pid: int = 0

    def age(self) -> float:
        """Seconds since this snapshot was taken."""
        return time.monotonic() - self.timestamp

    def is_stale(self, max_age: float = 2.0) -> bool:
        return self.age() > max_age

    def find_windows_by_app(self, app_id: str) -> list[WindowInfo]:
        """Case-insensitive app_id search."""
        needle = app_id.lower()
        return [w for w in self.windows if needle in w.app_id.lower()]

    def find_windows_by_title(self, fragment: str) -> list[WindowInfo]:
        """Case-insensitive title fragment search."""
        needle = fragment.lower()
        return [w for w in self.windows if needle in w.title.lower()]


# ---------------------------------------------------------------------------
# Backend adapters
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 2.0) -> str | None:
    """Run a command, return stdout or None on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _run_json(cmd: list[str], timeout: float = 2.0) -> dict | list | None:
    """Run a command expecting JSON output."""
    raw = _run(cmd, timeout)
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ── niri ──────────────────────────────────────────────────────────────────────

class _NiriBackend:
    """Uses `niri msg --json` IPC."""

    @staticmethod
    def available() -> bool:
        return shutil.which("niri") is not None and os.environ.get("WAYLAND_DISPLAY")

    @staticmethod
    def _output_origins() -> dict[str, tuple[int, int]]:
        """Map output name -> logical top-left origin, for multi-monitor offsets."""
        data = _run_json(["niri", "msg", "--json", "outputs"])
        origins: dict[str, tuple[int, int]] = {}
        if not isinstance(data, dict):
            return origins
        for name, info in data.items():
            logical = (info or {}).get("logical") or {}
            try:
                origins[name] = (int(logical.get("x", 0)), int(logical.get("y", 0)))
            except (TypeError, ValueError):
                origins[name] = (0, 0)
        return origins

    @staticmethod
    def _position_from_layout(
        layout: dict,
        workspace_id,
        workspaces: dict,
        origins: dict[str, tuple[int, int]],
    ) -> tuple[int, int] | None:
        """Absolute window origin, or None when niri does not report one.

        `tile_pos_in_workspace_view` is workspace-relative; add the output's
        logical origin so the result is in global screen coordinates. Older
        niri versions report null here — in that case we return None rather
        than invent (0, 0).
        """
        pos = (layout or {}).get("tile_pos_in_workspace_view")
        if not pos or len(pos) < 2:
            return None
        output = workspaces.get(workspace_id, "")
        ox, oy = origins.get(output, (0, 0))
        try:
            return (int(ox + pos[0]), int(oy + pos[1]))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _workspace_outputs() -> dict:
        data = _run_json(["niri", "msg", "--json", "workspaces"])
        if not isinstance(data, list):
            return {}
        return {ws.get("id"): ws.get("output", "") for ws in data}

    @staticmethod
    def get_windows() -> list[WindowInfo]:
        data = _run_json(["niri", "msg", "--json", "windows"])
        if not data or not isinstance(data, list):
            return []
        workspaces = _NiriBackend._workspace_outputs()
        origins = _NiriBackend._output_origins()
        result = []
        for w in data:
            layout = w.get("layout", {}) or {}
            ws = layout.get("window_size", [0, 0])
            workspace_id = w.get("workspace_id", 0)
            result.append(WindowInfo(
                window_id=w.get("id", 0),
                title=w.get("title", ""),
                app_id=w.get("app_id", ""),
                pid=w.get("pid", 0),
                workspace_id=workspace_id,
                is_focused=w.get("is_focused", False),
                is_floating=w.get("is_floating", False),
                size=(int(ws[0]), int(ws[1])) if len(ws) >= 2 else (0, 0),
                position=_NiriBackend._position_from_layout(
                    layout, workspace_id, workspaces, origins
                ),
                timestamp=time.monotonic(),
            ))
        return result

    @staticmethod
    def get_focused() -> WindowInfo | None:
        data = _run_json(["niri", "msg", "--json", "focused-window"])
        if not data or not isinstance(data, dict):
            return None
        layout = data.get("layout", {}) or {}
        ws = layout.get("window_size", [0, 0])
        workspace_id = data.get("workspace_id", 0)
        workspaces = _NiriBackend._workspace_outputs()
        origins = _NiriBackend._output_origins()
        return WindowInfo(
            window_id=data.get("id", 0),
            title=data.get("title", ""),
            app_id=data.get("app_id", ""),
            pid=data.get("pid", 0),
            workspace_id=workspace_id,
            is_focused=True,
            is_floating=data.get("is_floating", False),
            size=(int(ws[0]), int(ws[1])) if len(ws) >= 2 else (0, 0),
            position=_NiriBackend._position_from_layout(
                layout, workspace_id, workspaces, origins
            ),
            timestamp=time.monotonic(),
        )

    @staticmethod
    def get_workspaces() -> list[WorkspaceInfo]:
        data = _run_json(["niri", "msg", "--json", "workspaces"])
        if not data or not isinstance(data, list):
            return []
        return [
            WorkspaceInfo(
                workspace_id=ws.get("id", 0),
                index=ws.get("idx", 0),
                name=ws.get("name", ""),
                output=ws.get("output", ""),
                is_active=ws.get("is_active", False),
                is_focused=ws.get("is_focused", False),
                active_window_id=ws.get("active_window_id"),
            )
            for ws in data
        ]

    @staticmethod
    def focus_window(window_id: int) -> bool:
        r = _run(["niri", "msg", "action", "focus-window", "--id", str(window_id)])
        return r is not None

    @staticmethod
    def screenshot_screen(path: str) -> bool:
        """Capture entire screen using grim (reliable on Wayland), mss fallback."""
        # Prefer grim — produces real captures on niri/Wayland
        if shutil.which("grim"):
            r = _run(["grim", path], timeout=5.0)
            if r is not None and Path(path).exists():
                return True
        # Fallback: mss (may return black on some Wayland compositors)
        try:
            import mss as _mss
            with _mss.MSS() as sct:
                monitor = sct.monitors[1]  # primary
                img = sct.grab(monitor)
                _mss.tools.to_png(img.rgb, img.size, output=path)
                return Path(path).exists()
        except Exception:
            return False

    @staticmethod
    def screenshot_window(window_id: int | None, path: str) -> bool:
        """Window screenshot via niri IPC (may open interactive UI on some versions)."""
        cmd = ["niri", "msg", "action", "screenshot-window"]
        if window_id is not None:
            cmd.extend(["--id", str(window_id)])
        cmd.extend(["--path", path, "--write-to-disk", "true"])
        r = _run(cmd, timeout=5.0)
        return r is not None and Path(path).exists()


# ── Hyprland ──────────────────────────────────────────────────────────────────

class _HyprlandBackend:
    @staticmethod
    def available() -> bool:
        return (shutil.which("hyprctl") is not None
                and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") is not None)

    @staticmethod
    def get_windows() -> list[WindowInfo]:
        data = _run_json(["hyprctl", "clients", "-j"])
        if not data or not isinstance(data, list):
            return []
        return [
            WindowInfo(
                window_id=str(w.get("address", "")),
                title=w.get("title", ""),
                app_id=w.get("class", ""),
                pid=w.get("pid", 0),
                workspace_id=w.get("workspace", {}).get("id", 0),
                is_focused=w.get("focusHistoryID", -1) == 0,
                is_floating=w.get("floating", False),
                size=(w.get("size", [0, 0])[0], w.get("size", [0, 0])[1]),
                position=(w.get("at", [0, 0])[0], w.get("at", [0, 0])[1]),
                timestamp=time.monotonic(),
            )
            for w in data
        ]

    @staticmethod
    def get_focused() -> WindowInfo | None:
        data = _run_json(["hyprctl", "activewindow", "-j"])
        if not data or not isinstance(data, dict):
            return None
        return WindowInfo(
            window_id=str(data.get("address", "")),
            title=data.get("title", ""),
            app_id=data.get("class", ""),
            pid=data.get("pid", 0),
            workspace_id=data.get("workspace", {}).get("id", 0),
            is_focused=True,
            is_floating=data.get("floating", False),
            size=(data.get("size", [0, 0])[0], data.get("size", [0, 0])[1]),
            position=(data.get("at", [0, 0])[0], data.get("at", [0, 0])[1]),
            timestamp=time.monotonic(),
        )

    @staticmethod
    def get_workspaces() -> list[WorkspaceInfo]:
        data = _run_json(["hyprctl", "workspaces", "-j"])
        if not data or not isinstance(data, list):
            return []
        active = _run_json(["hyprctl", "activeworkspace", "-j"])
        active_id = active.get("id") if active else None
        return [
            WorkspaceInfo(
                workspace_id=ws.get("id", 0),
                index=ws.get("id", 0),
                name=ws.get("name", ""),
                output=ws.get("monitor", ""),
                is_active=ws.get("id") == active_id,
                is_focused=ws.get("id") == active_id,
            )
            for ws in data
        ]

    @staticmethod
    def focus_window(window_id: str) -> bool:
        r = _run(["hyprctl", "dispatch", "focuswindow", f"address:{window_id}"])
        return r is not None

    @staticmethod
    def screenshot_screen(path: str) -> bool:
        if shutil.which("grim"):
            r = _run(["grim", path], timeout=5.0)
            if r is not None and Path(path).exists():
                return True
        try:
            import mss as _mss
            with _mss.MSS() as sct:
                monitor = sct.monitors[1]
                img = sct.grab(monitor)
                _mss.tools.to_png(img.rgb, img.size, output=path)
                return Path(path).exists()
        except Exception:
            return False


# ── X11 / generic ─────────────────────────────────────────────────────────────

class _X11Backend:
    @staticmethod
    def available() -> bool:
        return (os.environ.get("DISPLAY") is not None
                and shutil.which("xdotool") is not None)

    @staticmethod
    def get_windows() -> list[WindowInfo]:
        raw = _run(["wmctrl", "-l", "-p"])
        if not raw:
            return []
        result = []
        for line in raw.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            wid, desktop, pid, _host, title = parts
            result.append(WindowInfo(
                window_id=wid,
                title=title,
                pid=int(pid) if pid.isdigit() else 0,
                workspace_id=int(desktop) if desktop.lstrip("-").isdigit() else 0,
                timestamp=time.monotonic(),
            ))
        return result

    @staticmethod
    def get_focused() -> WindowInfo | None:
        wid_hex = _run(["xdotool", "getactivewindow"])
        if not wid_hex:
            return None
        try:
            wid = int(wid_hex)
        except ValueError:
            return None
        name = _run(["xdotool", "getactivewindow", "getwindowname"]) or ""
        pid_s = _run(["xdotool", "getactivewindow", "getwindowpid"]) or "0"
        return WindowInfo(
            window_id=wid,
            title=name,
            pid=int(pid_s) if pid_s.isdigit() else 0,
            is_focused=True,
            timestamp=time.monotonic(),
        )

    @staticmethod
    def get_workspaces() -> list[WorkspaceInfo]:
        raw = _run(["wmctrl", "-d"])
        if not raw:
            return []
        result = []
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            result.append(WorkspaceInfo(
                workspace_id=int(parts[0]) if parts[0].isdigit() else 0,
                index=int(parts[0]) if parts[0].isdigit() else 0,
                is_active="*" in parts[1] if len(parts) > 1 else False,
                is_focused="*" in parts[1] if len(parts) > 1 else False,
                name=parts[-1] if len(parts) > 8 else "",
            ))
        return result

    @staticmethod
    def focus_window(window_id: int | str) -> bool:
        r = _run(["xdotool", "windowactivate", str(window_id)])
        return r is not None

    @staticmethod
    def screenshot_screen(path: str) -> bool:
        if shutil.which("grim"):
            r = _run(["grim", path], timeout=5.0)
            if r is not None and Path(path).exists():
                return True
        try:
            import mss as _mss
            with _mss.MSS() as sct:
                monitor = sct.monitors[1]
                img = sct.grab(monitor)
                _mss.tools.to_png(img.rgb, img.size, output=path)
                return Path(path).exists()
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Desktop Context Service (singleton)
# ---------------------------------------------------------------------------

class DesktopContextService:
    """
    Thread-safe, cached, compositor-aware desktop context.

    Usage:
        ctx = DesktopContextService()
        snap = ctx.snapshot()      # cached, auto-refreshes if stale
        snap = ctx.refresh()       # force fresh
        win = ctx.focused_window() # just the focused window
    """

    _instance = None
    _lock = Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._snap_lock = Lock()
        self._last_snap: DesktopSnapshot | None = None
        self._max_age = 1.5  # seconds before auto-refresh
        self._backend = None
        self._detect_environment()

    def _detect_environment(self) -> None:
        """Detect session type, compositor, and select backend."""
        self._session_type = SessionType.UNKNOWN
        self._compositor = Compositor.UNKNOWN
        self._compositor_name = ""
        self._desktop_env = os.environ.get("XDG_CURRENT_DESKTOP", "")

        session = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session == "wayland":
            self._session_type = SessionType.WAYLAND
        elif session == "x11":
            self._session_type = SessionType.X11
        elif os.environ.get("WAYLAND_DISPLAY"):
            self._session_type = SessionType.WAYLAND
        elif os.environ.get("DISPLAY"):
            self._session_type = SessionType.X11

        # Detect compositor
        if _NiriBackend.available():
            self._compositor = Compositor.NIRI
            self._compositor_name = "niri"
            self._backend = _NiriBackend
        elif _HyprlandBackend.available():
            self._compositor = Compositor.HYPRLAND
            self._compositor_name = "Hyprland"
            self._backend = _HyprlandBackend
        elif shutil.which("swaymsg") and os.environ.get("SWAYSOCK"):
            self._compositor = Compositor.SWAY
            self._compositor_name = "sway"
            # Could add a _SwayBackend, but the niri backend pattern is similar
        elif self._session_type == SessionType.WAYLAND:
            self._compositor = Compositor.GENERIC_WAYLAND
            self._compositor_name = self._desktop_env or "wayland"
        elif self._session_type == SessionType.X11 and _X11Backend.available():
            self._compositor = Compositor.X11
            self._compositor_name = "X11"
            self._backend = _X11Backend

        print(f"[DesktopCtx] Session: {self._session_type.name}, "
              f"Compositor: {self._compositor_name or 'unknown'}, "
              f"DE: {self._desktop_env or 'none'}")

    # ── Public API ─────────────────────────────────────────────────────────

    def snapshot(self, max_age: float | None = None) -> DesktopSnapshot:
        """Return cached snapshot, refreshing if stale."""
        age_limit = max_age if max_age is not None else self._max_age
        with self._snap_lock:
            if self._last_snap and not self._last_snap.is_stale(age_limit):
                return self._last_snap
        return self.refresh()

    def refresh(self) -> DesktopSnapshot:
        """Force a fresh desktop snapshot."""
        snap = DesktopSnapshot(
            timestamp=time.monotonic(),
            session_type=self._session_type,
            compositor=self._compositor,
            compositor_name=self._compositor_name,
            desktop_env=self._desktop_env,
        )

        if self._backend:
            try:
                snap.windows = self._backend.get_windows()
            except Exception as e:
                print(f"[DesktopCtx] get_windows error: {e}")

            try:
                focused = self._backend.get_focused()
                if focused:
                    snap.focused_window = focused
                elif snap.windows:
                    # Fall back to scanning windows list
                    for w in snap.windows:
                        if w.is_focused:
                            snap.focused_window = w
                            break
            except Exception as e:
                print(f"[DesktopCtx] get_focused error: {e}")

            try:
                snap.workspaces = self._backend.get_workspaces()
                for ws in snap.workspaces:
                    if ws.is_active or ws.is_focused:
                        snap.active_workspace_id = ws.workspace_id
                        break
            except Exception as e:
                print(f"[DesktopCtx] get_workspaces error: {e}")

        # Derive convenience fields
        if snap.focused_window:
            snap.active_app = snap.focused_window.app_id
            snap.active_title = snap.focused_window.title
            snap.active_pid = snap.focused_window.pid

        with self._snap_lock:
            self._last_snap = snap
        return snap

    def focused_window(self) -> WindowInfo | None:
        """Quick focused-window query (uses cached snapshot if fresh)."""
        snap = self.snapshot(max_age=1.0)
        return snap.focused_window

    def is_terminal_focused(self) -> bool:
        """Check if the focused window is a terminal emulator."""
        w = self.focused_window()
        if not w:
            return False
        term_apps = {"alacritty", "kitty", "foot", "wezterm", "gnome-terminal",
                     "konsole", "xfce4-terminal", "xterm", "tilix", "mate-terminal",
                     "lxterminal", "terminator", "sakura", "st", "urxvt"}
        return w.app_id.lower() in term_apps

    def find_terminal_windows(self) -> list[WindowInfo]:
        """List all terminal emulator windows."""
        snap = self.snapshot()
        term_apps = {"alacritty", "kitty", "foot", "wezterm", "gnome-terminal",
                     "konsole", "xfce4-terminal", "xterm", "tilix", "mate-terminal",
                     "lxterminal", "terminator", "sakura", "st", "urxvt"}
        return [w for w in snap.windows if w.app_id.lower() in term_apps]

    def find_browser_windows(self) -> list[WindowInfo]:
        """List all browser windows."""
        snap = self.snapshot()
        browser_apps = {"google-chrome", "chromium", "firefox", "brave-browser",
                        "microsoft-edge", "opera", "vivaldi", "epiphany",
                        "chromium-browser"}
        return [w for w in snap.windows
                if w.app_id.lower() in browser_apps
                or any(b in w.app_id.lower() for b in browser_apps)]

    def focus_window(self, window_id: int | str) -> bool:
        """Focus a window by its compositor ID."""
        if self._backend and hasattr(self._backend, "focus_window"):
            ok = self._backend.focus_window(window_id)
            if ok:
                self._last_snap = None  # invalidate cache
            return ok
        return False

    def screenshot(self, path: str | None = None,
                   window_id: int | str | None = None) -> str | None:
        """
        Take a screenshot. Returns the path to the PNG file, or None on failure.

        If window_id is provided and supported, captures that window.
        Otherwise captures the full screen.
        """
        if path is None:
            path = f"/tmp/jarvis_screenshot_{int(time.time() * 1000)}.png"

        if self._backend:
            if window_id is not None and hasattr(self._backend, "screenshot_window"):
                if self._backend.screenshot_window(window_id, path):
                    return path
            if hasattr(self._backend, "screenshot_screen"):
                if self._backend.screenshot_screen(path):
                    return path

        # Fallback: grim then mss
        if shutil.which("grim"):
            r = _run(["grim", path], timeout=5.0)
            if r is not None and Path(path).exists():
                return path
        try:
            import mss as _mss
            with _mss.MSS() as sct:
                monitor = sct.monitors[1]
                img = sct.grab(monitor)
                _mss.tools.to_png(img.rgb, img.size, output=path)
                if Path(path).exists():
                    return path
        except Exception:
            pass
        return None

    def ocr_screenshot(self, path: str | None = None,
                       window_id: int | str | None = None) -> str:
        """Take a screenshot and run OCR on it. Returns extracted text."""
        img_path = self.screenshot(path=path, window_id=window_id)
        if not img_path:
            return "[screenshot failed]"
        try:
            from PIL import Image
            import pytesseract
            img = Image.open(img_path)
            text = pytesseract.image_to_string(img)
            return text.strip() if text else "[no readable text found]"
        except ImportError:
            return "[pytesseract not available]"
        except Exception as e:
            return f"[OCR error: {e}]"

    def describe(self) -> str:
        """Human-readable summary of current desktop state."""
        snap = self.refresh()
        parts = [
            f"Session: {snap.session_type.name}",
            f"Compositor: {snap.compositor_name or 'unknown'}",
        ]
        if snap.focused_window:
            fw = snap.focused_window
            parts.append(f"Focused: {fw.app_id} — \"{fw.title}\"")
        else:
            parts.append("Focused: none")
        parts.append(f"Windows: {len(snap.windows)}")
        parts.append(f"Workspaces: {len(snap.workspaces)}")
        if snap.active_workspace_id is not None:
            parts.append(f"Active workspace: {snap.active_workspace_id}")
        return " | ".join(parts)

    @property
    def session_type(self) -> SessionType:
        return self._session_type

    @property
    def compositor(self) -> Compositor:
        return self._compositor

    @property
    def compositor_name(self) -> str:
        return self._compositor_name


# Module-level convenience
_service: DesktopContextService | None = None

def get_desktop_context() -> DesktopContextService:
    """Get or create the singleton desktop context service."""
    global _service
    if _service is None:
        _service = DesktopContextService()
    return _service
