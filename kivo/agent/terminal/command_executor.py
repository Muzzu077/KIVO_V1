"""Controlled subprocess execution runner with process isolation and timeout containment."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from pathlib import Path

from kivo.agent.terminal.command_policy import (
    CommandPolicyError,
    validate_terminal_command,
)
from kivo.agent.terminal.command_schemas import (
    CommandExecutionRequest,
    CommandExecutionResult,
)
from kivo.agent.terminal.path_validator import validate_working_directory

DEFAULT_COMMAND_TIMEOUT = 5.0
MAX_COMMAND_TIMEOUT = 10.0
MAX_OUTPUT_BYTES = 16384
MAX_OUTPUT_CHARS = 8000

SAFE_ENV_VARS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
)


def _kill_process_group(proc: asyncio.subprocess.Process | None) -> None:
    """Safely terminate the entire process group to ensure no children survive (SEC-03)."""
    if proc is not None and proc.pid:
        try:
            # Kill process group on POSIX
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass


async def _read_bounded_stream(
    reader: asyncio.StreamReader | None,
    max_bytes: int,
) -> tuple[bytes, bool]:
    """Read stream in bounded chunks without accumulating unlimited output in memory (SEC-02)."""
    if reader is None:
        return b"", False
    buffer = bytearray()
    truncated = False
    while True:
        chunk = await reader.read(1024)
        if not chunk:
            break
        remaining = max_bytes - len(buffer)
        if len(chunk) > remaining:
            buffer.extend(chunk[:remaining])
            truncated = True
            break
        buffer.extend(chunk)
    return bytes(buffer), truncated


def _get_sanitized_env() -> dict[str, str]:
    """Build a clean environment dictionary with no secrets or API keys."""
    env = {}
    for var in SAFE_ENV_VARS:
        if var in os.environ:
            env[var] = os.environ[var]
    if "PATH" not in env:
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return env


class CommandExecutor:
    """Safely executes allowlisted terminal commands via subprocess argument arrays."""

    def __init__(self, timeout: float = DEFAULT_COMMAND_TIMEOUT):
        self._timeout = min(max(1.0, timeout), MAX_COMMAND_TIMEOUT)

    async def execute(self, request: CommandExecutionRequest) -> CommandExecutionResult:
        """Validate and execute a terminal command with normal user privileges."""
        t0 = time.monotonic()

        # 1. Refuse execution if running as root
        if os.getuid() == 0:
            return CommandExecutionResult(
                command=request.command,
                arguments=request.arguments,
                working_directory=request.working_directory,
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time=0.0,
                success=False,
                error_message="Terminal execution as root/superuser is strictly refused.",
            )

        # 2. Validate policy & arguments
        try:
            cmd, validated_args = validate_terminal_command(request.command, request.arguments)
            cwd_path = validate_working_directory(request.working_directory)
        except Exception as exc:
            return CommandExecutionResult(
                command=request.command,
                arguments=request.arguments,
                working_directory=request.working_directory,
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time=round(time.monotonic() - t0, 3),
                success=False,
                error_message=str(exc),
            )

        # 3. Locate executable path on PATH
        exec_path = shutil.which(cmd)
        if not exec_path:
            return CommandExecutionResult(
                command=cmd,
                arguments=validated_args,
                working_directory=str(cwd_path),
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time=round(time.monotonic() - t0, 3),
                success=False,
                error_message=f"Executable '{cmd}' was not found on PATH.",
            )

        clean_env = _get_sanitized_env()
        proc = None

        try:
            # 4. Asynchronously launch subprocess with a new session for process group cleanup (SEC-03)
            proc = await asyncio.create_subprocess_exec(
                exec_path,
                *validated_args,
                cwd=str(cwd_path),
                env=clean_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

            # 5. Read stdout and stderr concurrently with strict bounded budget (SEC-02)
            stdout_task = asyncio.create_task(_read_bounded_stream(proc.stdout, MAX_OUTPUT_BYTES))
            stderr_task = asyncio.create_task(_read_bounded_stream(proc.stderr, MAX_OUTPUT_BYTES))

            (stdout_res, stderr_res) = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task),
                timeout=self._timeout,
            )
            stdout_bytes, out_truncated = stdout_res
            stderr_bytes, err_truncated = stderr_res

            # If either output stream hit the budget, terminate the process group
            if out_truncated or err_truncated:
                _kill_process_group(proc)

            # Wait and reap the process
            await proc.wait()

            execution_time = time.monotonic() - t0
            exit_code = proc.returncode if proc.returncode is not None else -1

            TRUNC_NOTICE = "\n[Output limit exceeded — truncated]"
            if out_truncated:
                cut = max(0, MAX_OUTPUT_CHARS - len(TRUNC_NOTICE))
                stdout_text = stdout_bytes.decode("utf-8", errors="replace")[:cut] + TRUNC_NOTICE
            else:
                stdout_text = stdout_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]

            ERR_TRUNC_NOTICE = "\n[Error output limit exceeded — truncated]"
            if err_truncated:
                cut = max(0, MAX_OUTPUT_CHARS - len(ERR_TRUNC_NOTICE))
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:cut] + ERR_TRUNC_NOTICE
            else:
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]

            return CommandExecutionResult(
                command=cmd,
                arguments=validated_args,
                working_directory=str(cwd_path),
                exit_code=exit_code,
                stdout=stdout_text,
                stderr=stderr_text,
                execution_time=round(execution_time, 3),
                success=exit_code == 0,
            )

        except asyncio.TimeoutError:
            if proc:
                _kill_process_group(proc)
                try:
                    await proc.wait()
                except Exception:
                    pass
            execution_time = time.monotonic() - t0
            return CommandExecutionResult(
                command=cmd,
                arguments=validated_args,
                working_directory=str(cwd_path),
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time=round(execution_time, 3),
                success=False,
                error_message=f"Command '{cmd}' execution timed out after {self._timeout}s.",
            )

        except asyncio.CancelledError:
            if proc:
                _kill_process_group(proc)
                try:
                    await proc.wait()
                except Exception:
                    pass
            raise

        except Exception as exc:
            if proc:
                _kill_process_group(proc)
                try:
                    await proc.wait()
                except Exception:
                    pass
            execution_time = time.monotonic() - t0
            return CommandExecutionResult(
                command=cmd,
                arguments=validated_args,
                working_directory=str(cwd_path),
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time=round(execution_time, 3),
                success=False,
                error_message=f"Subprocess execution error: {exc}",
            )
