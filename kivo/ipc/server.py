"""Unix-domain socket server for the KIVO local IPC protocol.

Security properties
    * AF_UNIX only, never a TCP listener.
    * The runtime directory must be owned by the current user and private.
    * The socket mode is 0600 and the containing directory is 0700.
    * A non-blocking ``flock`` lock file prevents two servers from racing.
    * Stale socket files are removed only after a failed connect probe and only
      when the file is a socket owned by the current user.
    * A non-socket file at the socket path is never overwritten.
"""
from __future__ import annotations

import errno
import fcntl
import os
import queue
import socket
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from kivo.ipc.protocol import (
    EVENT_NAME_SET,
    MAX_MESSAGE_BYTES,
    ProtocolError,
    decode_request,
    error_response,
    event_message,
    response,
)


class IPCError(Exception):
    """The IPC server cannot start or perform a safe operation."""


class AlreadyRunningError(IPCError):
    """Another server owns the socket or its startup lock."""


def default_socket_path() -> Path:
    """Return ``$XDG_RUNTIME_DIR/kivo/kivo.sock`` after validating the root.

    The runtime directory itself is owned by the session manager and must
    already exist.  Only its private ``kivo`` child is created by the server.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not runtime:
        raise IPCError(
            "XDG_RUNTIME_DIR is not set; cannot choose a private IPC socket path"
        )
    runtime_path = Path(runtime)
    if not runtime_path.is_absolute():
        raise IPCError("XDG_RUNTIME_DIR must be an absolute path")
    try:
        info = runtime_path.stat()
    except OSError as exc:
        raise IPCError(
            f"XDG_RUNTIME_DIR is not accessible: {runtime_path}: {exc}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise IPCError(f"XDG_RUNTIME_DIR is not a directory: {runtime_path}")
    if info.st_uid != os.getuid():
        raise IPCError(
            f"XDG_RUNTIME_DIR is not owned by the current user: {runtime_path}"
        )
    if info.st_mode & 0o077:
        raise IPCError(
            "XDG_RUNTIME_DIR must not be accessible by group/others: "
            f"{runtime_path}"
        )
    return runtime_path / "kivo" / "kivo.sock"


class IPCServer:
    """Threaded chunked request/event server on one Unix-domain socket."""

    MAX_CLIENT_QUEUE = 256
    ACCEPT_POLL_SECONDS = 0.25
    PROBE_TIMEOUT_SECONDS = 0.3

    def __init__(
        self,
        socket_path: str | Path | None = None,
        *,
        handlers: Any,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self._path_override = Path(socket_path) if socket_path is not None else None
        self._handlers = handlers
        self._logger = logger or (lambda _message: None)

        self._path: Optional[Path] = None
        self._lock_path: Optional[Path] = None
        self._lock_fd: Optional[int] = None
        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

        self._clients: set[_Client] = set()
        self._clients_lock = threading.Lock()
        self._client_threads: list[threading.Thread] = []
        self._sequence = 0
        self._sequence_lock = threading.Lock()

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def socket_path(self) -> Path:
        if self._path is not None:
            return self._path
        return self._path_override or default_socket_path()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def sequence(self) -> int:
        with self._sequence_lock:
            return self._sequence

    # ── Startup ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return

        path = self.socket_path
        encoded = os.fsencode(str(path))
        if len(encoded) > 107:
            raise IPCError(f"Unix socket path is too long ({len(encoded)} bytes)")

        self._prepare_directory(path)
        self._acquire_startup_lock(path)

        listener: Optional[socket.socket] = None
        bound = False
        try:
            self._remove_stale_socket(path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            bound = True
            os.chmod(path, 0o600)
            listener.listen(32)
            listener.settimeout(self.ACCEPT_POLL_SECONDS)
        except Exception:
            if listener is not None:
                listener.close()
            if bound:
                self._unlink_if_socket(path)
            self._release_startup_lock()
            raise

        self._listener = listener
        self._path = path
        self._stop_event.clear()
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="kivo-ipc-accept",
            daemon=True,
        )
        try:
            self._accept_thread.start()
        except Exception:
            # Do not leave a bound, unserved socket behind if the thread
            # cannot start (for example, thread exhaustion).
            self._running = False
            self._listener = None
            self._path = None
            self._accept_thread = None
            try:
                listener.close()
            except OSError:
                pass
            self._unlink_if_socket(path)
            self._release_startup_lock()
            raise
        self._logger(f"[IPC] Listening on {path}")

    def _prepare_directory(self, path: Path) -> None:
        parent = path.parent
        if self._path_override is None:
            # default_socket_path() has already validated $XDG_RUNTIME_DIR.
            # Create only its private "kivo" child, never intermediate
            # session directories.
            if not parent.exists():
                try:
                    parent.mkdir(mode=0o700)
                except OSError as exc:
                    raise IPCError(
                        f"cannot create IPC directory {parent}: {exc}"
                    ) from exc
        elif not parent.exists():
            # An explicit socket path must point into a directory the caller
            # has already prepared; never create arbitrary parents from it.
            raise IPCError(f"IPC parent directory does not exist: {parent}")

        try:
            info = parent.stat()
        except OSError as exc:
            raise IPCError(f"cannot inspect IPC directory {parent}: {exc}") from exc

        if not stat.S_ISDIR(info.st_mode):
            raise IPCError(f"IPC parent is not a directory: {parent}")
        if info.st_uid != os.getuid():
            raise IPCError(
                f"IPC directory is not owned by the current user: {parent}"
            )
        if info.st_mode & 0o077:
            raise IPCError(
                f"IPC directory must not be accessible by group/others: {parent}"
            )

    def _acquire_startup_lock(self, path: Path) -> None:
        lock_path = path.with_name(path.name + ".lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        for _attempt in range(5):
            try:
                fd = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise IPCError(f"cannot create IPC lock {lock_path}: {exc}") from exc

            try:
                os.chmod(lock_path, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise AlreadyRunningError(
                        "another KIVO IPC server is already starting or running"
                    ) from exc
                raise IPCError(
                    f"cannot lock IPC startup file {lock_path}: {exc}"
                ) from exc

            # A stopping server may have unlinked the previous lock inode
            # between our open() and flock(). Only accept the lock if the path
            # still names the inode we actually locked.
            try:
                path_info = os.stat(lock_path)
                fd_info = os.fstat(fd)
            except FileNotFoundError:
                os.close(fd)
                continue
            except OSError as exc:
                os.close(fd)
                raise IPCError(
                    f"cannot validate IPC lock {lock_path}: {exc}"
                ) from exc

            if (path_info.st_dev, path_info.st_ino) == (
                    fd_info.st_dev, fd_info.st_ino):
                self._lock_path = lock_path
                self._lock_fd = fd
                return

            os.close(fd)

        raise IPCError(f"could not acquire a stable IPC startup lock {lock_path}")

    def _remove_stale_socket(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise IPCError(f"cannot inspect existing IPC socket {path}: {exc}") from exc

        if not stat.S_ISSOCK(info.st_mode):
            raise IPCError(
                f"refusing to replace non-socket file at IPC path: {path}"
            )
        if info.st_uid != os.getuid():
            raise IPCError(f"IPC socket is owned by another user: {path}")

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(self.PROBE_TIMEOUT_SECONDS)
        try:
            probe.connect(str(path))
        except OSError as exc:
            if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                raise IPCError(f"cannot probe existing IPC socket {path}: {exc}") from exc
        else:
            raise AlreadyRunningError(
                "another KIVO IPC server is already accepting connections"
            )
        finally:
            probe.close()

        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise IPCError(f"cannot remove stale IPC socket {path}: {exc}") from exc

    # ── Accept / clients ───────────────────────────────────────────────────

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop_event.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            client = _Client(self, conn)
            with self._clients_lock:
                self._clients.add(client)
            thread = threading.Thread(
                target=client.run,
                name=f"kivo-ipc-client-{id(client):x}",
                daemon=True,
            )
            with self._clients_lock:
                self._client_threads.append(thread)
            thread.start()

    def _forget_client(self, client: "_Client") -> None:
        with self._clients_lock:
            self._clients.discard(client)

    def _dispatch(self, client: "_Client", request) -> None:
        operation = request.operation
        if operation == "subscribe_events":
            events = client.subscribe(request.params.get("events"))
            client.send(response(request.request_id, {
                "subscribed": True,
                "events": events,
            }))
            return
        if operation == "unsubscribe_events":
            client.unsubscribe()
            client.send(response(request.request_id, {
                "subscribed": False,
                "events": [],
            }))
            return

        op_name = operation.replace(".", "_")
        handler = getattr(self._handlers, op_name, None)
        if handler is None or not callable(handler):
            client.send(error_response(
                request.request_id,
                "unsupported_operation",
                f"operation '{operation}' is not available",
            ))
            return

        try:
            if request.params:
                try:
                    result = handler(request.params)
                except TypeError:
                    result = handler()
            else:
                result = handler()
        except Exception as exc:  # noqa: BLE001 - never leak tracebacks to clients
            self._logger(f"[IPC] handler {operation} failed: {exc}")
            client.send(error_response(
                request.request_id,
                "internal_error",
                "the backend could not complete the request",
            ))
            return

        client.send(response(request.request_id, result))

    # ── Events ─────────────────────────────────────────────────────────────

    def publish(self, event: str, data: dict[str, Any]) -> int:
        """Send an event to every subscribed client.

        Returns the assigned sequence number, or 0 when the server is stopped.
        Delivery is best-effort: a client whose bounded send queue is full is
        disconnected so a slow frontend can never block backend workers.
        """
        if event not in EVENT_NAME_SET:
            raise ValueError(f"unknown event: {event}")
        if not isinstance(data, dict):
            raise TypeError("event data must be a dict")
        if not self._running:
            return 0

        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence

        message = event_message(event, data, sequence)
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            if client.wants(event):
                client.send(message)
        return sequence

    # ── Shutdown ───────────────────────────────────────────────────────────

    def stop(self) -> None:
        if not self._running and self._listener is None and self._lock_fd is None:
            return

        self._running = False
        self._stop_event.set()

        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
            threads = list(self._client_threads)
            self._client_threads.clear()
        for client in clients:
            client.close()

        accept_thread, self._accept_thread = self._accept_thread, None
        if accept_thread is not None and accept_thread.is_alive():
            accept_thread.join(timeout=2.0)

        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=2.0)

        path = self._path
        self._path = None
        if path is not None:
            self._unlink_if_socket(path)

        self._release_startup_lock()

    def _unlink_if_socket(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return
        try:
            path.unlink()
        except OSError:
            pass

    def _release_startup_lock(self) -> None:
        fd = self._lock_fd
        lock_path = self._lock_path
        self._lock_fd = None
        self._lock_path = None
        if fd is None:
            return

        if lock_path is not None:
            try:
                path_info = lock_path.stat()
                fd_info = os.fstat(fd)
                if (path_info.st_dev, path_info.st_ino) == (fd_info.st_dev, fd_info.st_ino):
                    lock_path.unlink()
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


class _Client:
    """One local frontend connection: reader thread + bounded writer queue."""

    def __init__(self, server: IPCServer, conn: socket.socket):
        self._server = server
        self._conn = conn
        self._queue: queue.Queue[Optional[bytes]] = queue.Queue(
            maxsize=server.MAX_CLIENT_QUEUE
        )
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._sub_lock = threading.Lock()
        self._subscribed = False
        self._event_filter: Optional[frozenset[str]] = None
        self._writer: Optional[threading.Thread] = None

    # ── Subscription state ─────────────────────────────────────────────────

    def subscribe(self, events: Optional[list[str]]) -> list[str]:
        with self._sub_lock:
            self._subscribed = True
            self._event_filter = frozenset(events) if events else None
            if self._event_filter is None:
                from kivo.ipc.protocol import EVENT_NAMES
                return list(EVENT_NAMES)
            return sorted(self._event_filter)

    def unsubscribe(self) -> None:
        with self._sub_lock:
            self._subscribed = False
            self._event_filter = None

    def wants(self, event: str) -> bool:
        with self._sub_lock:
            if not self._subscribed or self._closed.is_set():
                return False
            return self._event_filter is None or event in self._event_filter

    # ── Send side ──────────────────────────────────────────────────────────

    def send(self, message: bytes) -> bool:
        if self._closed.is_set():
            return False
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            self._server._logger("[IPC] client send queue full; disconnecting")
            self.close()
            return False
        return True

    def send_now(self, message: bytes) -> bool:
        """Send a final error synchronously before a forced disconnect."""
        if self._closed.is_set():
            return False
        with self._send_lock:
            try:
                self._conn.sendall(message)
                return True
            except OSError:
                return False

    def _writer_loop(self) -> None:
        while not self._closed.is_set():
            try:
                message = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if message is None:
                return
            with self._send_lock:
                try:
                    self._conn.sendall(message)
                except OSError:
                    self.close()
                    return

    # ── Read side ──────────────────────────────────────────────────────────

    def run(self) -> None:
        self._writer = threading.Thread(
            target=self._writer_loop,
            name=f"kivo-ipc-writer-{id(self):x}",
            daemon=True,
        )
        self._writer.start()

        reader = None
        try:
            reader = self._conn.makefile("rb")
            while not self._closed.is_set():
                raw = reader.readline(MAX_MESSAGE_BYTES + 2)
                if not raw:
                    break
                body = raw[:-1] if raw.endswith(b"\n") else raw
                if len(body) > MAX_MESSAGE_BYTES:
                    self.send_now(error_response(
                        None,
                        "message_too_large",
                        f"message exceeds {MAX_MESSAGE_BYTES} bytes",
                    ))
                    break
                if not raw.endswith(b"\n"):
                    # EOF in the middle of a frame.
                    break
                try:
                    request = decode_request(body)
                except ProtocolError as exc:
                    message = error_response(
                        getattr(exc, "request_id", None),
                        exc.code,
                        exc.message,
                    )
                    if exc.close:
                        self.send_now(message)
                        break
                    self.send(message)
                    continue

                try:
                    self._server._dispatch(self, request)
                except Exception as exc:  # noqa: BLE001
                    self._server._logger(f"[IPC] request handling failed: {exc}")
                    self.send(error_response(
                        request.request_id,
                        "internal_error",
                        "the backend could not complete the request",
                    ))
        except (OSError, ValueError):
            pass
        finally:
            if reader is not None:
                try:
                    reader.close()
                except OSError:
                    pass
            self.close()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._conn.close()
        except OSError:
            pass
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._server._forget_client(self)

        writer = self._writer
        if writer is not None and writer is not threading.current_thread():
            if writer.is_alive():
                writer.join(timeout=1.0)
