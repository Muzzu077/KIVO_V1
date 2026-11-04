"""Strict security policy and per-command argument allowlists for terminal execution."""
from __future__ import annotations

import re
from typing import Callable, Optional

from kivo.agent.terminal.command_schemas import CommandPolicyError
from kivo.agent.terminal.path_validator import sanitize_path_argument

MAX_ARGUMENT_COUNT = 15
MAX_ARGUMENT_LENGTH = 256

# Strict allowlist of permitted read-only inspection executables
ALLOWLISTED_COMMANDS: frozenset[str] = frozenset({
    "pwd",
    "ls",
    "stat",
    "file",
    "df",
    "du",
    "free",
    "uname",
    "whoami",
    "id",
    "uptime",
    "ps",
})

# Forbidden commands explicitly blocked with clear error explanations
FORBIDDEN_COMMANDS: frozenset[str] = frozenset({
    "sudo",
    "doas",
    "pkexec",
    "su",
    "rm",
    "mv",
    "cp",
    "chmod",
    "chown",
    "chgrp",
    "kill",
    "killall",
    "pkill",
    "pacman",
    "yay",
    "systemctl",
    "reboot",
    "shutdown",
    "poweroff",
    "bash",
    "sh",
    "zsh",
    "python",
    "python3",
    "node",
    "perl",
    "ruby",
    "curl",
    "wget",
    "nc",
    "ncat",
    "netcat",
    "socat",
    "dd",
    "mkfs",
    "find",
})

# Metacharacters that could indicate command injection attempts
DANGEROUS_METACHARACTERS = re.compile(r"[;|<>&>$`\n\r\t\\]")


def _validate_pwd(args: list[str]) -> list[str]:
    for a in args:
        if a not in ("-L", "-P"):
            raise CommandPolicyError(f"pwd does not accept option '{a}'.")
    return args


def _validate_ls(args: list[str]) -> list[str]:
    # Disallow 'R' to prevent uncontrolled recursive directory scans (SEC-06)
    valid_flag_chars = set("lahstr1FpdABCmxvUkgn")
    cleaned = []
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            flag_chars = a[1:]
            if "R" in flag_chars:
                raise CommandPolicyError("ls recursive flag '-R' is prohibited for security.")
            if not flag_chars or not set(flag_chars).issubset(valid_flag_chars):
                raise CommandPolicyError(f"ls option '{a}' contains unpermitted flags.")
            cleaned.append(a)
        elif a.startswith("--"):
            if a not in ("--color=never", "--color=auto", "--group-directories-first"):
                raise CommandPolicyError(f"ls option '{a}' is not permitted.")
            cleaned.append(a)
        else:
            # Path argument
            cleaned.append(sanitize_path_argument(a))
    return cleaned


def _validate_stat(args: list[str]) -> list[str]:
    cleaned = []
    for a in args:
        if a in ("-c", "--format", "-L", "-t", "-f", "--file-system", "--printf"):
            cleaned.append(a)
        elif a.startswith("-"):
            if not all(c in "cLtf" for c in a[1:]):
                raise CommandPolicyError(f"stat option '{a}' is not permitted.")
            cleaned.append(a)
        else:
            cleaned.append(sanitize_path_argument(a))
    return cleaned


def _validate_file(args: list[str]) -> list[str]:
    valid_flag_chars = set("biLzkN")
    cleaned = []
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            if not all(c in valid_flag_chars for c in a[1:]):
                raise CommandPolicyError(f"file option '{a}' is not permitted.")
            cleaned.append(a)
        elif a in ("--brief", "--mime", "--mime-type"):
            cleaned.append(a)
        else:
            cleaned.append(sanitize_path_argument(a))
    return cleaned


def _validate_df(args: list[str]) -> list[str]:
    valid_flag_chars = set("hkmTtxPl")
    cleaned = []
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            if not all(c in valid_flag_chars for c in a[1:]):
                raise CommandPolicyError(f"df option '{a}' is not permitted.")
            cleaned.append(a)
        elif a in ("--human-readable", "--total", "--portability"):
            cleaned.append(a)
        else:
            cleaned.append(sanitize_path_argument(a))
    return cleaned


def _validate_du(args: list[str]) -> list[str]:
    valid_flag_chars = set("hskmbcd")
    cleaned = []
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            if not all(c in valid_flag_chars or c.isdigit() for c in a[1:]):
                raise CommandPolicyError(f"du option '{a}' is not permitted.")
            cleaned.append(a)
        elif a.startswith("--max-depth="):
            depth_val = a.split("=", 1)[1]
            if not depth_val.isdigit() or int(depth_val) > 3:
                raise CommandPolicyError("du --max-depth must be a positive integer <= 3.")
            cleaned.append(a)
        elif a in ("--human-readable", "--summarize", "--total"):
            cleaned.append(a)
        else:
            cleaned.append(sanitize_path_argument(a))
    return cleaned


def _validate_free(args: list[str]) -> list[str]:
    valid_flag_chars = set("hkmgbtwl")
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            if not all(c in valid_flag_chars for c in a[1:]):
                raise CommandPolicyError(f"free option '{a}' is not permitted.")
        elif a not in ("--human", "--total", "--wide", "--mega", "--giga", "--tera"):
            raise CommandPolicyError(f"free option '{a}' is not permitted.")
    return args


def _validate_uname(args: list[str]) -> list[str]:
    valid_flag_chars = set("asnmrvpio")
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            if not all(c in valid_flag_chars for c in a[1:]):
                raise CommandPolicyError(f"uname option '{a}' is not permitted.")
        elif a not in ("--all", "--kernel-name", "--nodename", "--kernel-release", "--kernel-version", "--machine", "--processor", "--operating-system"):
            raise CommandPolicyError(f"uname option '{a}' is not permitted.")
    return args


def _validate_whoami(args: list[str]) -> list[str]:
    if args:
        raise CommandPolicyError("whoami does not accept arguments.")
    return []


def _validate_id(args: list[str]) -> list[str]:
    valid_flag_chars = set("ugGnzr")
    cleaned = []
    for a in args:
        if a.startswith("-"):
            if not all(c in valid_flag_chars for c in a[1:]):
                raise CommandPolicyError(f"id option '{a}' is not permitted.")
            cleaned.append(a)
        elif re.match(r"^[a-zA-Z0-9_-]{1,32}$", a):
            cleaned.append(a)
        else:
            raise CommandPolicyError(f"Invalid username argument for id: '{a}'.")
    return cleaned


def _validate_uptime(args: list[str]) -> list[str]:
    for a in args:
        if a not in ("-p", "-s", "-h", "--pretty", "--since"):
            raise CommandPolicyError(f"uptime option '{a}' is not permitted.")
    return args


def _validate_ps(args: list[str]) -> list[str]:
    # Allow safe read-only process listing flags
    cleaned = []
    valid_standalone = {
        "aux", "ax", "-ef", "-e", "-A", "-u", "-p", "u", "x", "-f", "-l",
        "-H", "--forest", "-w", "-ww", "-o", "--sort", "T", "r"
    }
    for a in args:
        if a in valid_standalone:
            cleaned.append(a)
        elif a.startswith("--sort=") and re.match(r"^--sort=[+\-]?[%a-zA-Z0-9_,]+$", a):
            cleaned.append(a)
        elif a.startswith("-o") and re.match(r"^-o\s*[%a-zA-Z0-9_,]+$", a):
            cleaned.append(a)
        elif a.startswith("-") and all(c in "efAluxHw" for c in a[1:]):
            cleaned.append(a)
        elif re.match(r"^[a-zA-Z0-9_-]{1,32}$", a):
            cleaned.append(a)
        else:
            raise CommandPolicyError(f"ps option '{a}' is not permitted.")
    return cleaned


VALIDATORS: dict[str, Callable[[list[str]], list[str]]] = {
    "pwd": _validate_pwd,
    "ls": _validate_ls,
    "stat": _validate_stat,
    "file": _validate_file,
    "df": _validate_df,
    "du": _validate_du,
    "free": _validate_free,
    "uname": _validate_uname,
    "whoami": _validate_whoami,
    "id": _validate_id,
    "uptime": _validate_uptime,
    "ps": _validate_ps,
}


def validate_terminal_command(command: str, arguments: list[str]) -> tuple[str, list[str]]:
    """Validate executable and arguments against strict security allowlists.

    Returns normalized (command, arguments) or raises CommandPolicyError.
    """
    clean_cmd = (command or "").strip().lower()
    if not clean_cmd:
        raise CommandPolicyError("Command cannot be empty.")

    # 1. Block explicitly forbidden commands with tailored message
    if clean_cmd in FORBIDDEN_COMMANDS:
        raise CommandPolicyError(
            f"Command '{clean_cmd}' is prohibited. Stage 10 only allows read-only system inspection commands."
        )

    # 2. Check allowlist
    if clean_cmd not in ALLOWLISTED_COMMANDS:
        raise CommandPolicyError(
            f"Command '{clean_cmd}' is not in the read-only allowlist. "
            f"Allowed commands: {', '.join(sorted(ALLOWLISTED_COMMANDS))}."
        )

    # 3. Check arguments limits
    args = list(arguments or [])
    if len(args) > MAX_ARGUMENT_COUNT:
        raise CommandPolicyError(f"Too many arguments ({len(args)} > max {MAX_ARGUMENT_COUNT}).")

    for a in args:
        if not isinstance(a, str):
            raise CommandPolicyError("All command arguments must be strings.")
        if len(a) > MAX_ARGUMENT_LENGTH:
            raise CommandPolicyError(f"Argument '{a[:20]}...' exceeds maximum length {MAX_ARGUMENT_LENGTH}.")
        if DANGEROUS_METACHARACTERS.search(a):
            raise CommandPolicyError(f"Argument '{a}' contains forbidden shell metacharacters.")

    # 4. Executable-specific validation
    validator = VALIDATORS.get(clean_cmd)
    if validator is not None:
        validated_args = validator(args)
    else:
        validated_args = args

    return clean_cmd, validated_args
