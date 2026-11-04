"""Data schemas and models for controlled terminal command execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


class TerminalSecurityError(Exception):
    """Base exception for terminal security violations."""


class CommandPolicyError(TerminalSecurityError):
    """Raised when an executable or argument violates security policy."""


class PathValidationError(TerminalSecurityError):
    """Raised when a path or working directory is invalid or unsafe."""


@dataclass(frozen=True)
class CommandExecutionRequest:
    """Validated terminal command execution request."""

    command: str
    arguments: list[str] = field(default_factory=list)
    working_directory: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CommandExecutionResult:
    """Outcome of a controlled subprocess execution."""

    command: str
    arguments: list[str]
    working_directory: str
    exit_code: int
    stdout: str
    stderr: str
    execution_time: float
    success: bool
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "arguments": list(self.arguments),
            "working_directory": self.working_directory,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "execution_time": round(self.execution_time, 3),
            "success": self.success,
            "error_message": self.error_message,
        }
