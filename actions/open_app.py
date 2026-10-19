import time
import subprocess
import platform
import shutil

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_SYSTEM = platform.system()

_APP_ALIASES: dict[str, dict[str, str]] = {

    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome-stable"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome-stable"},
    "google-chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome-stable"},
    "google-chrome-stable": {"Windows": "chrome",                "Darwin": "Google Chrome",        "Linux": "google-chrome-stable"},
    "browser":            {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome-stable"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",                  "Darwin": "Safari",               "Linux": "firefox"},
    "opera":              {"Windows": "opera",                   "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",                 "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",                   "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",                  "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",                 "Darwin": "Netflix",              "Linux": "firefox"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "wt",                      "Darwin": "Terminal",             "Linux": "alacritty"},
    "alacritty":          {"Windows": "alacritty",               "Darwin": "alacritty",            "Linux": "alacritty"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "alacritty"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "alacritty"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",                "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",                 "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "firefox"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "firefox"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "epic":               {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
}


def _normalize(raw: str) -> str:
    key = raw.lower().strip()

    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(_SYSTEM, raw)

    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(_SYSTEM, raw)

    return raw  

from pathlib import Path


def _launch_windows(app_name: str, path: str = "", command: str = "") -> bool:
    app_lower = app_name.lower().strip()
    if any(term in app_lower for term in ("terminal", "cmd", "powershell", "wt")):
        if shutil.which("wt"):
            args = ["wt"]
            if path:
                args.extend(["-d", path])
            if command:
                args.extend(["cmd.exe", "/k", command])
            try:
                subprocess.Popen(args, shell=False)
                return True
            except Exception:
                pass
        cmd_str = f'start cmd.exe /k "{"cd /d " + path + " && " if path else ""}{command}"'
        try:
            subprocess.Popen(cmd_str, shell=True)
            return True
        except Exception:
            pass

    if shutil.which(app_name) or shutil.which(app_name.split(".")[0]):
        try:
            cmd = [app_name]
            if path:
                cmd.append(path)
            subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.0)
            return True
        except Exception as e:
            print(f"[open_app] subprocess failed: {e}")

    if ":" in app_name:
        try:
            subprocess.Popen(f"start {app_name}", shell=True)
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        import pyautogui
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.7)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.9)
        pyautogui.press("enter")
        time.sleep(2.0)
        return True
    except Exception as e:
        print(f"[open_app] Start Menu search failed: {e}")

    return False


def _launch_macos(app_name: str, path: str = "", command: str = "") -> bool:
    app_lower = app_name.lower().strip()
    if any(term in app_lower for term in ("terminal", "iterm")):
        shell_script = f"cd '{path}' && " if path else ""
        if command:
            shell_script += f"{command}; "
        shell_script += "exec $SHELL"
        script = f'tell application "Terminal" to do script "{shell_script}"'
        try:
            subprocess.Popen(["osascript", "-e", script])
            return True
        except Exception:
            pass

    try:
        args = ["open", "-a", app_name]
        if path:
            args.append(path)
        result = subprocess.run(args, capture_output=True, timeout=8)
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    try:
        args = ["open", "-a", f"{app_name}.app"]
        if path:
            args.append(path)
        result = subprocess.run(args, capture_output=True, timeout=8)
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    binary = shutil.which(app_name) or shutil.which(app_name.lower())
    if binary:
        try:
            cmd = [binary]
            if path:
                cmd.append(path)
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(1.5)
        return True
    except Exception as e:
        print(f"[open_app] Spotlight failed: {e}")

    return False


_LINUX_TERMINAL_FALLBACKS = [
    "alacritty", "kitty", "foot", "wezterm",
    "gnome-terminal", "konsole", "xfce4-terminal",
    "x-terminal-emulator", "xterm", "lxterminal", "mate-terminal", "tilix",
]

_LINUX_BROWSER_FALLBACKS = [
    "google-chrome-stable", "google-chrome", "chromium",
    "chromium-browser", "brave-browser", "firefox",
]


def _launch_linux(app_name: str, path: str = "", command: str = "") -> bool:
    app_lower = app_name.lower().strip()

    # 1. Resolve working directory / file path if provided
    resolved_path = None
    if path:
        p_str = path.strip().strip('"').strip("'")
        shortcuts = {
            "desktop": Path.home() / "Desktop",
            "downloads": Path.home() / "Downloads",
            "documents": Path.home() / "Documents",
            "home": Path.home(),
            "~": Path.home(),
        }
        if p_str.lower() in shortcuts:
            resolved_path = shortcuts[p_str.lower()]
        else:
            resolved_path = Path(p_str).expanduser().resolve()

    # 2. Terminal launching with working directory and command support
    if any(term in app_lower for term in ("terminal", "alacritty", "kitty", "konsole", "xterm", "bash", "cmd", "foot", "wezterm", "gnome-terminal")):
        for term in _LINUX_TERMINAL_FALLBACKS:
            bin_path = shutil.which(term)
            if not bin_path:
                continue

            cmd = [bin_path]
            if term == "alacritty":
                if resolved_path and resolved_path.is_dir():
                    cmd.extend(["--working-directory", str(resolved_path)])
                if command:
                    cmd.extend(["-e", "bash", "-c", f"{command}; exec bash"])
            elif term == "kitty":
                if resolved_path and resolved_path.is_dir():
                    cmd.extend(["--directory", str(resolved_path)])
                if command:
                    cmd.extend(["bash", "-c", f"{command}; exec bash"])
            elif term == "gnome-terminal":
                if resolved_path and resolved_path.is_dir():
                    cmd.append(f"--working-directory={resolved_path}")
                if command:
                    cmd.extend(["--", "bash", "-c", f"{command}; exec bash"])
            elif term == "konsole":
                if resolved_path and resolved_path.is_dir():
                    cmd.extend(["--workdir", str(resolved_path)])
                if command:
                    cmd.extend(["-e", "bash", "-c", f"{command}; exec bash"])
            elif term == "xfce4-terminal":
                if resolved_path and resolved_path.is_dir():
                    cmd.extend(["--working-directory", str(resolved_path)])
                if command:
                    cmd.extend(["-e", f"bash -c '{command}; exec bash'"])
            else:
                shell_exec = ""
                if resolved_path and resolved_path.is_dir():
                    shell_exec += f"cd '{resolved_path}' && "
                if command:
                    shell_exec += f"{command}; "
                shell_exec += "exec bash"
                cmd.extend(["-e", "bash", "-c", shell_exec])

            try:
                subprocess.Popen(
                    cmd,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.5)
                return True
            except Exception as e:
                print(f"[open_app] terminal {term} launch failed: {e}")
                continue

    # 3. Web browser launching (Chrome, Chromium, Firefox, etc.)
    if any(b in app_lower for b in ("chrome", "chromium", "browser", "firefox", "brave", "opera", "edge")):
        for b in _LINUX_BROWSER_FALLBACKS:
            b_path = shutil.which(b)
            if b_path:
                cmd = [b_path]
                if path:
                    cmd.append(path)
                try:
                    subprocess.Popen(
                        cmd,
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    time.sleep(0.5)
                    return True
                except Exception as e:
                    print(f"[open_app] browser {b} failed: {e}")

    # 4. Direct binary execution
    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-")) or
        shutil.which(app_name.lower().replace(" ", "_"))
    )
    if binary:
        cmd = [binary]
        if resolved_path:
            cmd.append(str(resolved_path))
        try:
            subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
            return True
        except Exception:
            pass

    # 5. Desktop file launch via gtk-launch or gio
    for desktop_name in [
        app_name.lower(),
        app_name.lower().replace(" ", "-"),
        app_name.lower().replace(" ", ""),
    ]:
        try:
            res = subprocess.run(["gtk-launch", desktop_name], capture_output=True, timeout=2)
            if res.returncode == 0:
                return True
        except Exception:
            pass

    # 6. File / Folder / URL launching via xdg-open
    target_to_open = resolved_path or (Path(app_name).expanduser() if Path(app_name).expanduser().exists() else None)
    if target_to_open and target_to_open.exists():
        try:
            subprocess.Popen(
                ["xdg-open", str(target_to_open)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            pass

    if app_name.startswith("http://") or app_name.startswith("https://"):
        try:
            subprocess.Popen(
                ["xdg-open", app_name],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            pass

    return False


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}


def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    app_name = params.get("app_name", "").strip()
    path = (
        params.get("path", "").strip() or
        params.get("working_directory", "").strip() or
        params.get("directory", "").strip()
    )
    command = params.get("command", "").strip()

    if not app_name and path:
        app_name = path
        path = ""

    if not app_name:
        return "No application or target provided, sir."

    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return f"Unsupported operating system: {_SYSTEM}"

    normalized = _normalize(app_name)
    print(f"[open_app] Launching: '{app_name}' → '{normalized}' (path='{path}', cmd='{command}', os={_SYSTEM})")

    if player:
        player.write_log(f"[open_app] {app_name}")

    try:
        if launcher(normalized, path=path, command=command):
            target_info = f" in {path}" if path else ""
            if command:
                target_info += f" running '{command}'"
            return f"Opened {app_name}{target_info}, sir."
        if normalized.lower() != app_name.lower():
            if launcher(app_name, path=path, command=command):
                return f"Opened {app_name}, sir."
        return (
            f"Could not confirm that {app_name} launched. "
            f"It may still be loading, or it might not be installed."
        )
    except Exception as e:
        print(f"[open_app] Error: {e}")
        return f"Failed to open {app_name}: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "open_app",
    "description": "Opens any application, file, or browser on the computer, or launches a terminal in a specific directory or running a command. Use whenever the user asks to open apps (e.g. Chrome, Terminal, VSCode), open files, or open terminal and change to a directory.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Exact name of the application (e.g. 'Chrome', 'terminal', 'Alacritty', 'VSCode')"
            },
            "path": {
                "type": "STRING",
                "description": "Optional working directory path (for terminal) or file path or URL to open (e.g. '~/Projects/Mark-LV', 'Downloads', 'https://google.com')"
            },
            "command": {
                "type": "STRING",
                "description": "Optional shell command to execute inside the terminal (e.g. 'git status', 'ls -la')"
            }
        },
        "required": [
            "app_name"
        ]
    },
    "handler": open_app,
}
