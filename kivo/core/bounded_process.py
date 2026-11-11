"""Bounded, argv-only subprocess execution for KIVO.

The runner never invokes a shell, never accepts a command string, and always
drains both pipes: output beyond the caller's cap is discarded rather than
leaving a child blocked on a full pipe.  A timed-out child is killed with its
process group so helper processes cannot survive it.
"""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

_READ_CHUNK = 65536
_KILL_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class BoundedProcessResult:
    """Result of a bounded subprocess run.

    ``stdout`` and ``stderr`` are decoded with replacement so malformed bytes
    cannot raise in a telemetry sampling thread.  ``*_truncated`` records that
    a pipe exceeded its cap even though the child was allowed to exit.
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def _validate_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise ValueError("argv must be a sequence of arguments, not a command string")
    args = list(argv)
    if not args:
        raise ValueError("argv must not be empty")
    for argument in args:
        if not isinstance(argument, str):
            raise ValueError("argv entries must be strings")
        if not argument or "\x00" in argument:
            raise ValueError("argv entries must be non-empty and NUL-free")
    return args


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=_KILL_GRACE_SECONDS)
    except Exception:
        pass


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    stdout_limit: int = 16 * 1024,
    stderr_limit: int = 4 * 1024,
    env: Mapping[str, str] | None = None,
    input_data: bytes | str | None = None,
) -> BoundedProcessResult:
    """Run ``argv`` with a deadline, bounded pipes and no shell.

    Raises ``ValueError`` for an invalid command and ``OSError`` if the
    executable cannot be spawned.  Every other failure is represented in the
    returned result so callers can translate it into a telemetry status.
    """
    args = _validate_argv(argv)
    timeout = float(timeout)
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if stdout_limit <= 0 or stderr_limit <= 0:
        raise ValueError("output limits must be positive")

    child_env = dict(os.environ)
    if env:
        child_env.update({str(key): str(value) for key, value in env.items()})

    input_bytes = (
        input_data.encode("utf-8")
        if isinstance(input_data, str)
        else (bytes(input_data) if input_data is not None else None)
    )

    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
        shell=False,
        close_fds=True,
        start_new_session=True,
    )

    stdout_pipe = proc.stdout
    stderr_pipe = proc.stderr
    stdin_pipe = proc.stdin
    if stdout_pipe is None or stderr_pipe is None:
        _kill_process_group(proc)
        return BoundedProcessResult(
            returncode=1,
            stdout="",
            stderr="pipes unavailable",
        )

    stdout_fd = stdout_pipe.fileno()
    stderr_fd = stderr_pipe.fileno()
    limits = {stdout_fd: stdout_limit, stderr_fd: stderr_limit}
    buffers: dict[int, bytearray] = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    truncated = {stdout_fd: False, stderr_fd: False}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    timed_out = False

    stdin_fd = None
    stdin_offset = 0
    if input_bytes is not None and stdin_pipe is not None:
        stdin_fd = stdin_pipe.fileno()
        os.set_blocking(stdin_fd, False)

    try:
        for descriptor in buffers:
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)

        if stdin_fd is not None and input_bytes:
            selector.register(stdin_fd, selectors.EVENT_WRITE)
        elif stdin_pipe is not None:
            stdin_pipe.close()
            stdin_pipe = None

        while True:
            if not selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break

            events = selector.select(timeout=min(remaining, 0.1))
            for key, _ in events:
                descriptor = key.fd

                # Handle non-blocking stdin writing
                if descriptor == stdin_fd and stdin_pipe is not None and input_bytes is not None:
                    try:
                        written = os.write(stdin_fd, input_bytes[stdin_offset:])
                        stdin_offset += written
                        if stdin_offset >= len(input_bytes):
                            try:
                                selector.unregister(stdin_fd)
                            except Exception:
                                pass
                            try:
                                stdin_pipe.close()
                            except Exception:
                                pass
                            stdin_pipe = None
                            stdin_fd = None
                    except BlockingIOError:
                        pass
                    except OSError:
                        try:
                            selector.unregister(stdin_fd)
                        except Exception:
                            pass
                        try:
                            stdin_pipe.close()
                        except Exception:
                            pass
                        stdin_pipe = None
                        stdin_fd = None
                    continue

                try:
                    chunk = os.read(descriptor, _READ_CHUNK)
                except BlockingIOError:
                    continue
                except OSError:
                    try:
                        selector.unregister(descriptor)
                    except Exception:
                        pass
                    continue
                if not chunk:
                    try:
                        selector.unregister(descriptor)
                    except Exception:
                        pass
                    continue

                buffer = buffers[descriptor]
                limit = limits[descriptor]
                if len(buffer) + len(chunk) > limit:
                    truncated[descriptor] = True
                    room = max(0, limit - len(buffer))
                    if room:
                        buffer.extend(chunk[:room])
                else:
                    buffer.extend(chunk)
    finally:
        if timed_out:
            _kill_process_group(proc)
        if stdin_pipe is not None:
            try:
                stdin_pipe.close()
            except Exception:
                pass
        try:
            selector.close()
        except Exception:
            pass
        try:
            stdout_pipe.close()
        except Exception:
            pass
        try:
            stderr_pipe.close()
        except Exception:
            pass

    return BoundedProcessResult(
        returncode=proc.returncode if proc.returncode is not None else 1,
        stdout=bytes(buffers[stdout_fd]).decode("utf-8", errors="replace"),
        stderr=bytes(buffers[stderr_fd]).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        stdout_truncated=truncated[stdout_fd],
        stderr_truncated=truncated[stderr_fd],
    )


__all__ = ["BoundedProcessResult", "run_bounded"]
