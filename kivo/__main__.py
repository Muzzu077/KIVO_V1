#!/usr/bin/env python3
"""KIVO command-line entry point.

Usage:
    python -m kivo              Start the backend
    python -m kivo --version    Show the KIVO version
    python -m kivo --check      Non-destructive environment check
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `python -m kivo` from a source checkout without installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kivo.app import run  # noqa: E402  (path setup must happen first)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if not args:
        return run()
    if args[0] in ("--version", "-V"):
        show_version()
        return 0
    if args[0] in ("--check", "-c"):
        return 0 if check_env() else 1
    if args[0] in ("--help", "-h"):
        show_help()
        return 0

    print(f"Unknown command: {' '.join(args)}")
    print("Available commands:", file=sys.stderr)
    print("  --version    Show version information", file=sys.stderr)
    print("  --check      Check environment and configuration", file=sys.stderr)
    return 2


def show_version() -> None:
    from kivo import __version__

    print(f"KIVO {__version__}")
    print("Dynamic AI Assistant for Arch Linux/Wayland")
    print("Backend mode ready for Quickshell integration")


def show_help() -> None:
    print(__doc__.strip())
    print()
    print("  -h, --help       Show this help")
    print("  -V, --version    Show version information")
    print("  -c, --check      Check environment and configuration")


def check_env() -> bool:
    """Inspect modules, configuration and desktop integration.

    Read-only: no microphone is opened, no desktop action is executed, no
    configuration directory is created and no socket is bound.
    """
    print("Checking KIVO environment...")
    ok = True

    print(f"  Python {sys.version.split()[0]}")
    if sys.version_info < (3, 10):
        print("  FAIL Python 3.10 or newer is required")
        ok = False
    else:
        print("  OK   Python version")

    required_modules = [
        "memory.config_manager",
        "memory.memory_manager",
        "core.brain",
        "core.screen_context",
        "core.action_loader",
        "core.drive",
        "core.task_orchestrator",
        "core.action_policy",
        "core.confirm",
        "core.agent_workspace",
        "kivo.app",
    ]
    print("  Required modules:")
    failed_modules = []
    for module_name in required_modules:
        try:
            __import__(module_name)
        except Exception as exc:  # noqa: BLE001 - report any import failure
            failed_modules.append((module_name, str(exc)))
            print(f"    FAIL {module_name}: {exc}")
        else:
            print(f"    OK   {module_name}")
    if failed_modules:
        ok = False

    actions_dir = _REPO_ROOT / "actions"
    action_files = sorted(actions_dir.glob("*.py")) if actions_dir.is_dir() else []
    if action_files:
        print(f"  OK   actions directory ({len(action_files)} Python files)")
    else:
        print(f"  FAIL actions directory missing or empty: {actions_dir}")
        ok = False

    _check_configuration()
    _check_desktop()
    _check_runtime_dir()

    print()
    if ok:
        print("Environment check completed; KIVO can start.")
    else:
        print("Environment check found blocking problems (see FAIL above).")
    return ok


def _check_configuration() -> None:
    """Report the existing config location without creating or editing it."""
    try:
        from memory.config_manager import CONFIG_FILE, config_exists, is_configured
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN configuration module unavailable: {exc}")
        return

    if not config_exists():
        print(f"  WARN no configuration file at {CONFIG_FILE}")
        print("       KIVO will start, but model-backed features stay disabled")
        return

    if is_configured():
        print(f"  OK   configuration at {CONFIG_FILE} (API key present)")
    else:
        print(f"  WARN configuration at {CONFIG_FILE} has no usable API key")


def _check_desktop() -> None:
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    niri_socket = os.environ.get("NIRI_SOCKET", "")

    if session_type == "wayland" or wayland_display:
        print("  OK   Wayland session detected")
    else:
        print("  WARN Wayland session not detected; desktop integration may be unavailable")

    if niri_socket:
        print(f"  OK   niri socket: {niri_socket}")
    else:
        print("  WARN NIRI_SOCKET is not set; niri integration may be unavailable")


def _check_runtime_dir() -> None:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime:
        print("  WARN XDG_RUNTIME_DIR is not set")
        return
    runtime_path = Path(runtime)
    if runtime_path.is_dir() and os.access(runtime_path, os.W_OK):
        print(f"  OK   runtime directory writable: {runtime_path}")
        try:
            from kivo.ipc.server import default_socket_path
            print(f"  OK   IPC socket path: {default_socket_path()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN IPC socket path unavailable: {exc}")
    else:
        print(f"  WARN runtime directory is missing or not writable: {runtime_path}")


if __name__ == "__main__":
    sys.exit(main())
