"""Two-tier quarantine storage and safe publication into Downloads.

Security properties
--------------------
* Quarantine roots are opened component-by-component with ``O_NOFOLLOW`` and
  must be owned by the current user with mode 0700.
* Files use random internal names, ``O_EXCL | O_NOFOLLOW``, and mode 0600.
* All file operations are relative to an already-open directory descriptor.
* Incoming display names are never used as quarantine paths.
* Reservation accounting is atomic and bounded by the aggregate quota.
* Publication uses ``renameat2(RENAME_NOREPLACE)`` on the same filesystem and
  an O_EXCL temporary file, incremental re-hash, fsync, and no-overwrite
  rename across filesystems.
* Cleanup only removes regular ``quarantine_*.part`` files owned by the
  current user from KIVO's dedicated quarantine directories.  No recursive
  deletion is performed.

TLS 1.3 protects transfer contents in transit.  Quarantine data is **not**
encrypted at rest; confidentiality relies on 0600/0700 and the user account.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import platform
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from kivo.files.service import _get_mount_id

from kivo.bridge.transfer_validation import (
    MAX_AGGREGATE_RESERVED_BYTES,
    MAX_UPLOAD_BYTES,
    MIB,
    GIB,
    validate_display_filename,
)

QUARANTINE_DIR_NAME = "quarantine"
QUARANTINE_FILE_PREFIX = "quarantine_"
QUARANTINE_FILE_SUFFIX = ".part"
QUARANTINE_TIER_RUNTIME = "runtime"
QUARANTINE_TIER_CACHE = "cache"
QUARANTINE_TIER_SPLIT_BYTES = 50 * MIB
TMPFS_SAFETY_MARGIN_BYTES = 128 * MIB
DISK_SAFETY_MARGIN_BYTES = 1 * GIB
MAX_QUARANTINE_FILE_ATTEMPTS = 32
MAX_PUBLISH_ATTEMPTS = 1000
COPY_CHUNK_BYTES = 64 * 1024
MAX_COLLISION_SUFFIX = 9999

_RENAME_NOREPLACE = 1
_RENAMEAT2_SYSCALLS = {
    "x86_64": 316,
    "amd64": 316,
    "aarch64": 276,
    "arm64": 276,
    "riscv64": 276,
    "i386": 353,
    "i686": 353,
    "armv7l": 382,
    "ppc64le": 357,
    "s390x": 347,
}
_RENAMEAT2_UNAVAILABLE_ERRNOS = frozenset(
    {
        errno.ENOSYS,
        errno.EINVAL,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    }
)


class QuarantineError(Exception):
    """A structured and safe quarantine refusal or failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class DirectoryIdentity:
    dev: int
    ino: int
    mnt_id: int

    @property
    def token(self) -> str:
        return f"dev:{self.dev}:ino:{self.ino}:mnt:{self.mnt_id}"


@dataclass
class QuarantineRoot:
    tier: str
    path: Path
    fd: int
    identity: DirectoryIdentity
    safety_margin: int


@dataclass
class Reservation:
    size: int
    tier: str
    released: bool = False


@dataclass
class QuarantineHandle:
    tier: str
    root: QuarantineRoot
    name: str
    fd: Optional[int]
    identity: DirectoryIdentity
    size: int
    reservation: Reservation
    sealed: bool = False
    published: bool = False
    discarded: bool = False


@dataclass(frozen=True)
class PublishedFile:
    name: str
    size: int
    sha256: bytes
    cross_filesystem: bool


def _current_uid() -> int:
    return os.getuid()


def _close(fd: Optional[int]) -> None:
    if isinstance(fd, int) and fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


def _map_os_error(exc: OSError, *, code: str = "quarantine_failed") -> QuarantineError:
    err = getattr(exc, "errno", None)
    if err in (errno.ENOSPC, errno.EDQUOT):
        return QuarantineError("insufficient_storage", "The filesystem has no space left.")
    if err in (errno.EACCES, errno.EPERM):
        return QuarantineError("permission_denied", "The filesystem denied access.")
    if err == errno.ELOOP:
        return QuarantineError("quarantine_root_unsafe", "A symbolic link was encountered.")
    if err == errno.ENOTDIR:
        return QuarantineError("quarantine_root_unsafe", "A path component is not a directory.")
    if err == errno.ENOENT:
        return QuarantineError("quarantine_unavailable", "A required path does not exist.")
    if err == errno.EROFS:
        return QuarantineError("read_only_filesystem", "The filesystem is read-only.")
    if err == errno.ENAMETOOLONG:
        return QuarantineError("invalid_filename", "The target name is too long.")
    return QuarantineError(code, str(exc) or "The quarantine operation failed.")


def _fsync(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise QuarantineError(
            "durability_failed", "The file could not be synchronized to disk."
        ) from exc


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except OSError as exc:
            raise _map_os_error(exc, code="write_failed") from exc
        if written <= 0:
            raise QuarantineError("write_failed", "The file could not be written completely.")
        view = view[written:]


def _read_exact(fd: int, length: int) -> bytes:
    parts: list[bytes] = []
    remaining = length
    while remaining > 0:
        try:
            chunk = os.read(fd, min(65536, remaining))
        except OSError as exc:
            raise _map_os_error(exc, code="read_failed") from exc
        if not chunk:
            raise QuarantineError(
                "unexpected_eof", "The quarantine file ended before the expected contents."
            )
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _hash_descriptor(fd: int, limit: int) -> tuple[int, bytes]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise _map_os_error(exc, code="read_failed") from exc
    digest = hashlib.sha256()
    total = 0
    while True:
        try:
            chunk = os.read(fd, 64 * 1024)
        except OSError as exc:
            raise _map_os_error(exc, code="read_failed") from exc
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise QuarantineError(
                "file_modified", "The quarantine file is larger than its declared size."
            )
        digest.update(chunk)
    return total, digest.digest()


def _validate_regular_file(
    st: os.stat_result,
    *,
    identity: Optional[DirectoryIdentity] = None,
    expected_uid: Optional[int] = None,
    expected_mode: Optional[int] = None,
) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise QuarantineError("unsupported_file_type", "The quarantine path is not a regular file.")
    if expected_uid is not None and st.st_uid != expected_uid:
        raise QuarantineError("unsafe_owner", "The quarantine file is not owned by the current user.")
    if st.st_nlink != 1:
        raise QuarantineError("unsafe_link_count", "The quarantine file has unexpected hard links.")
    if expected_mode is not None and stat.S_IMODE(st.st_mode) != expected_mode:
        raise QuarantineError("unsafe_permissions", "The quarantine file permissions are unsafe.")
    if identity is not None:
        if (st.st_dev, st.st_ino) != (identity.dev, identity.ino):
            raise QuarantineError("file_replaced", "The quarantine file was replaced.")


def _secure_directory(path: Path, *, create: bool) -> tuple[int, DirectoryIdentity]:
    """Open a private quarantine directory without following any symlink.

    The final directory is forced to mode 0700 and must be owned by the current
    user.  Intermediate components are traversed descriptor-relatively with
    ``O_NOFOLLOW``; missing components are created with mode 0700.
    """
    path = Path(path)
    if not path.is_absolute():
        raise QuarantineError("quarantine_root_unsafe", "Quarantine roots must be absolute.")
    if os.path.normpath(str(path)) != str(path):
        raise QuarantineError(
            "quarantine_root_unsafe", "Quarantine roots must be normalized absolute paths."
        )

    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                if not create:
                    raise QuarantineError(
                        "quarantine_unavailable", "A quarantine directory does not exist."
                    )
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise _map_os_error(exc) from exc
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=fd,
                    )
                except OSError as exc:
                    raise _map_os_error(exc) from exc
            except OSError as exc:
                raise _map_os_error(exc) from exc

            _close(fd)
            fd = child

        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise QuarantineError("quarantine_root_unsafe", "A quarantine root is not a directory.")
        if st.st_uid != _current_uid():
            raise QuarantineError(
                "quarantine_root_unsafe",
                "A quarantine root is not owned by the current user.",
            )
        try:
            os.fchmod(fd, 0o700)
        except OSError as exc:
            raise _map_os_error(exc) from exc
        st = os.fstat(fd)
        if stat.S_IMODE(st.st_mode) != 0o700:
            raise QuarantineError(
                "quarantine_root_unsafe", "A quarantine root could not be secured to mode 0700."
            )
        mnt_id = _get_mount_id(fd)
        if mnt_id is None:
            raise QuarantineError(
                "mount_identity_unavailable",
                "The quarantine filesystem mount identity could not be verified.",
            )
        return fd, DirectoryIdentity(dev=st.st_dev, ino=st.st_ino, mnt_id=mnt_id)
    except Exception:
        _close(fd)
        raise


def _open_existing_directory(path: Path, *, private: bool) -> tuple[int, DirectoryIdentity]:
    """Open an existing directory for publication without following symlinks."""
    path = Path(path)
    if not path.is_absolute():
        raise QuarantineError("downloads_unavailable", "The Downloads path must be absolute.")
    if os.path.normpath(str(path)) != str(path):
        raise QuarantineError("downloads_unavailable", "The Downloads path must be normalized.")

    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except OSError as exc:
                raise _map_os_error(exc, code="downloads_unavailable") from exc
            _close(fd)
            fd = child

        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise QuarantineError("downloads_unavailable", "Downloads is not a directory.")
        if st.st_uid != _current_uid():
            raise QuarantineError(
                "downloads_unavailable", "Downloads is not owned by the current user."
            )
        if st.st_mode & 0o022:
            raise QuarantineError(
                "downloads_unavailable",
                "Downloads must not be group- or world-writable.",
            )
        if private and stat.S_IMODE(st.st_mode) != 0o700:
            raise QuarantineError("quarantine_root_unsafe", "The directory is not private.")
        mnt_id = _get_mount_id(fd)
        if mnt_id is None:
            raise QuarantineError(
                "mount_identity_unavailable",
                "The destination filesystem mount identity could not be verified.",
            )
        return fd, DirectoryIdentity(dev=st.st_dev, ino=st.st_ino, mnt_id=mnt_id)
    except Exception:
        _close(fd)
        raise


_RENAMEAT2_FUNC = None
_RENAMEAT2_LOADED = False


def _load_renameat2():
    global _RENAMEAT2_FUNC, _RENAMEAT2_LOADED
    if _RENAMEAT2_LOADED:
        return _RENAMEAT2_FUNC
    _RENAMEAT2_LOADED = True
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except Exception:
        return None

    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        _RENAMEAT2_FUNC = renameat2
        return _RENAMEAT2_FUNC

    syscall_number = _RENAMEAT2_SYSCALLS.get(platform.machine().lower())
    if syscall_number is None or not hasattr(libc, "syscall"):
        return None

    libc.syscall.restype = ctypes.c_long

    def _syscall_renameat2(
        old_dir_fd: int,
        old_name: bytes,
        new_dir_fd: int,
        new_name: bytes,
        flags: int,
    ) -> int:
        return int(
            libc.syscall(
                syscall_number,
                ctypes.c_int(old_dir_fd),
                ctypes.c_char_p(old_name),
                ctypes.c_int(new_dir_fd),
                ctypes.c_char_p(new_name),
                ctypes.c_uint(flags),
            )
        )

    _RENAMEAT2_FUNC = _syscall_renameat2
    return _RENAMEAT2_FUNC


def _renameat2_noreplace(
    old_dir_fd: int,
    old_name: str,
    new_dir_fd: int,
    new_name: str,
) -> None:
    """Atomically install ``old_name`` as ``new_name``; never overwrite."""
    func = _load_renameat2()
    if func is None:
        raise QuarantineError(
            "no_overwrite_unsupported",
            "Atomic no-overwrite publication is unavailable on this system.",
        )
    result = func(
        old_dir_fd,
        os.fsencode(old_name),
        new_dir_fd,
        os.fsencode(new_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        err = ctypes.get_errno()
        if err in _RENAMEAT2_UNAVAILABLE_ERRNOS:
            raise QuarantineError(
                "no_overwrite_unsupported",
                "Atomic no-overwrite publication is unavailable for this filesystem.",
            )
        raise OSError(err, os.strerror(err))


def _default_runtime_base() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if value and os.path.isabs(value):
        return Path(value)
    import tempfile

    return Path(tempfile.gettempdir()) / f"kivo-{os.getuid()}"


def _default_cache_base() -> Path:
    value = os.environ.get("XDG_CACHE_HOME")
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / ".cache"


def _read_user_dirs_download() -> Optional[Path]:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home and os.path.isabs(config_home):
        base = Path(config_home)
    else:
        base = Path.home() / ".config"
    config_file = base / "user-dirs.dirs"
    try:
        with open(config_file, "r", encoding="utf-8") as handle:
            content = handle.read(64 * 1024 + 1)
    except (OSError, UnicodeError):
        return None
    if len(content) > 64 * 1024:
        return None
    home = str(Path.home())
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if not stripped.startswith("XDG_DOWNLOAD_DIR"):
            continue
        _, _, raw = stripped.partition("=")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = raw[1:-1]
        if not raw:
            continue
        expanded = raw.replace("${HOME}", home).replace("$HOME", home)
        candidate = Path(expanded).expanduser()
        if candidate.is_absolute():
            return candidate
    return None


def _candidate_names(display_name: str):
    """Yield collision-safe names: report.pdf, report (1).pdf, ..."""
    name = validate_display_filename(display_name)
    yield name
    suffix = Path(name).suffix
    if suffix and suffix != name:
        stem = name[: -len(suffix)]
    else:
        stem = name
        suffix = ""
    for index in range(1, MAX_PUBLISH_ATTEMPTS):
        yield f"{stem} ({index}){suffix}"


class QuarantineManager:
    """Owns quarantine roots, reservations, files, and Downloads publication."""

    def __init__(
        self,
        *,
        runtime_base: Path | str | None = None,
        cache_base: Path | str | None = None,
        downloads_dir: Path | str | None = None,
        free_space_fn: Callable[[QuarantineRoot], int] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._runtime_base = Path(runtime_base) if runtime_base is not None else _default_runtime_base()
        self._cache_base = Path(cache_base) if cache_base is not None else _default_cache_base()
        self._downloads_override = Path(downloads_dir) if downloads_dir is not None else None
        self._free_space_fn = free_space_fn
        self._clock = clock
        self._lock = threading.RLock()
        self._roots: dict[str, QuarantineRoot] = {}
        self._reserved_bytes = 0
        self._started = False
        self._closed = False

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved_bytes

    @property
    def is_started(self) -> bool:
        with self._lock:
            return self._started and not self._closed

    def start(self) -> None:
        """Create/open both roots, verify them, and remove abandoned parts."""
        with self._lock:
            if self._closed:
                raise QuarantineError("quarantine_unavailable", "Quarantine is closed.")
            if self._started:
                return
            roots: dict[str, QuarantineRoot] = {}
            try:
                for tier, base in (
                    (QUARANTINE_TIER_RUNTIME, self._runtime_base),
                    (QUARANTINE_TIER_CACHE, self._cache_base),
                ):
                    path = base / "kivo" / "bridge" / QUARANTINE_DIR_NAME
                    fd, identity = _secure_directory(path, create=True)
                    margin = (
                        TMPFS_SAFETY_MARGIN_BYTES
                        if tier == QUARANTINE_TIER_RUNTIME
                        else DISK_SAFETY_MARGIN_BYTES
                    )
                    roots[tier] = QuarantineRoot(
                        tier=tier,
                        path=path,
                        fd=fd,
                        identity=identity,
                        safety_margin=margin,
                    )
            except Exception:
                for root in roots.values():
                    _close(root.fd)
                raise

            self._roots = roots
            self._started = True
            self._cleanup_abandoned_locked()

    def close(self) -> None:
        """Close root descriptors.  All transfers must already be discarded."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            for root in self._roots.values():
                _close(root.fd)
            self._roots.clear()
            self._reserved_bytes = 0

    def tier_for_size(self, size: int) -> str:
        return (
            QUARANTINE_TIER_RUNTIME
            if size <= QUARANTINE_TIER_SPLIT_BYTES
            else QUARANTINE_TIER_CACHE
        )

    def _root_for_tier(self, tier: str) -> QuarantineRoot:
        root = self._roots.get(tier)
        if root is None:
            raise QuarantineError("quarantine_unavailable", "Quarantine storage is unavailable.")
        try:
            current = os.stat(root.path, follow_symlinks=False)
        except OSError as exc:
            raise _map_os_error(exc, code="quarantine_unavailable") from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_uid != _current_uid()
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            raise QuarantineError(
                "quarantine_root_replaced", "The quarantine root was replaced or is unsafe."
            )
        if (current.st_dev, current.st_ino) != (root.identity.dev, root.identity.ino):
            raise QuarantineError(
                "quarantine_root_replaced", "The quarantine root identity changed."
            )
        mnt_id = _get_mount_id(root.fd)
        if mnt_id is None or mnt_id != root.identity.mnt_id:
            raise QuarantineError(
                "quarantine_root_replaced", "The quarantine mount identity changed."
            )
        return root

    def _free_bytes_locked(self, root: QuarantineRoot) -> int:
        if self._free_space_fn is not None:
            try:
                return max(0, int(self._free_space_fn(root)))
            except Exception as exc:  # noqa: BLE001 - fail closed on a broken probe
                raise QuarantineError(
                    "storage_probe_failed", "Free space could not be determined."
                ) from exc
        try:
            st = os.fstatvfs(root.fd)
        except OSError as exc:
            raise _map_os_error(exc, code="storage_probe_failed") from exc
        return int(st.f_bavail) * int(st.f_frsize)

    def check_free_space(self, tier: str, remaining_bytes: int) -> None:
        with self._lock:
            root = self._root_for_tier(tier)
            required = max(0, int(remaining_bytes)) + root.safety_margin
            if self._free_bytes_locked(root) < required:
                raise QuarantineError(
                    "insufficient_storage",
                    "Insufficient free space including the safety margin.",
                )

    def reserve(self, size: int, tier: str | None = None) -> Reservation:
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise QuarantineError("invalid_size", "The declared size is invalid.")
        if size > MAX_UPLOAD_BYTES:
            raise QuarantineError("size_exceeds_limit", "The file exceeds the 2 GiB limit.")
        selected = tier or self.tier_for_size(size)
        with self._lock:
            if not self._started or self._closed:
                raise QuarantineError(
                    "quarantine_unavailable", "Quarantine storage is not running."
                )
            if self._reserved_bytes + size > MAX_AGGREGATE_RESERVED_BYTES:
                raise QuarantineError(
                    "aggregate_capacity_exceeded",
                    "Aggregate quarantine reservations exceed the 2 GiB limit.",
                )
            root = self._root_for_tier(selected)
            required = size + root.safety_margin
            if self._free_bytes_locked(root) < required:
                raise QuarantineError(
                    "insufficient_storage",
                    "Insufficient free space including the safety margin.",
                )
            self._reserved_bytes += size
            return Reservation(size=size, tier=selected)

    def release(self, reservation: Reservation | None) -> None:
        if reservation is None:
            return
        with self._lock:
            if reservation.released:
                return
            reservation.released = True
            self._reserved_bytes = max(0, self._reserved_bytes - reservation.size)

    def create_file(self, reservation: Reservation) -> QuarantineHandle:
        with self._lock:
            if not self._started or self._closed:
                raise QuarantineError(
                    "quarantine_unavailable", "Quarantine storage is not running."
                )
            root = self._root_for_tier(reservation.tier)
            uid = _current_uid()
            for _ in range(MAX_QUARANTINE_FILE_ATTEMPTS):
                name = (
                    QUARANTINE_FILE_PREFIX
                    + secrets.token_hex(16)
                    + QUARANTINE_FILE_SUFFIX
                )
                try:
                    fd = os.open(
                        name,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | os.O_CLOEXEC,
                        0o600,
                        dir_fd=root.fd,
                    )
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise _map_os_error(exc) from exc

                try:
                    try:
                        os.fchmod(fd, 0o600)
                    except OSError as exc:
                        raise _map_os_error(exc) from exc
                    st = os.fstat(fd)
                    _validate_regular_file(st, expected_uid=uid, expected_mode=0o600)
                    mnt_id = _get_mount_id(fd)
                    if mnt_id is None or mnt_id != root.identity.mnt_id:
                        raise QuarantineError(
                            "cross_device_boundary",
                            "The quarantine file was created on an unexpected mount.",
                        )
                    if st.st_dev != root.identity.dev:
                        raise QuarantineError(
                            "cross_device_boundary",
                            "The quarantine file was created on an unexpected device.",
                        )
                    identity = DirectoryIdentity(
                        dev=st.st_dev, ino=st.st_ino, mnt_id=mnt_id
                    )
                    return QuarantineHandle(
                        tier=root.tier,
                        root=root,
                        name=name,
                        fd=fd,
                        identity=identity,
                        size=reservation.size,
                        reservation=reservation,
                    )
                except Exception:
                    _close(fd)
                    try:
                        os.unlink(name, dir_fd=root.fd)
                    except OSError:
                        pass
                    raise
            raise QuarantineError(
                "temp_name_exhausted", "A unique quarantine filename could not be created."
            )

    def append_chunk(
        self,
        handle: QuarantineHandle,
        data: bytes,
        *,
        expected_offset: int,
        expected_total_size: int,
    ) -> int:
        """Append one already-authenticated chunk and return the new offset."""
        with self._lock:
            if handle.fd is None or handle.sealed or handle.discarded or handle.published:
                raise QuarantineError("invalid_transfer_state", "The quarantine file is not writable.")
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise QuarantineError("invalid_chunk", "The chunk is empty.")
            if len(data) > 1024 * 1024:
                raise QuarantineError("chunk_too_large", "The chunk exceeds the fixed 1 MiB size.")
            new_offset = expected_offset + len(data)
            if new_offset > expected_total_size:
                raise QuarantineError(
                    "chunk_length_mismatch", "The chunk exceeds the declared file size."
                )
            root = self._root_for_tier(handle.tier)
            remaining_after = expected_total_size - new_offset
            required = remaining_after + root.safety_margin
            if self._free_bytes_locked(root) < required:
                raise QuarantineError(
                    "insufficient_storage",
                    "Free space fell below the required safety margin.",
                )
            try:
                st_before = os.fstat(handle.fd)
            except OSError as exc:
                raise _map_os_error(exc) from exc
            _validate_regular_file(st_before, identity=handle.identity, expected_uid=_current_uid())
            if st_before.st_size != expected_offset:
                raise QuarantineError(
                    "file_modified", "The quarantine file changed during the transfer."
                )
            try:
                os.lseek(handle.fd, expected_offset, os.SEEK_SET)
            except OSError as exc:
                raise _map_os_error(exc) from exc
            _write_all(handle.fd, bytes(data))
            try:
                st_after = os.fstat(handle.fd)
            except OSError as exc:
                raise _map_os_error(exc) from exc
            if st_after.st_size != new_offset:
                raise QuarantineError(
                    "write_failed", "The quarantine file did not reach the expected size."
                )
            if (st_after.st_dev, st_after.st_ino) != (handle.identity.dev, handle.identity.ino):
                raise QuarantineError("file_replaced", "The quarantine file was replaced.")
            return new_offset

    def chunk_digest(self, handle: QuarantineHandle, offset: int, length: int) -> bytes:
        """Hash a previously committed range to make duplicate chunks idempotent."""
        with self._lock:
            fd = handle.fd
            close_after = False
            if fd is None:
                fd = self._open_handle_locked(handle, writable=False)
                close_after = True
            try:
                try:
                    os.lseek(fd, offset, os.SEEK_SET)
                except OSError as exc:
                    raise _map_os_error(exc) from exc
                data = _read_exact(fd, length)
                return hashlib.sha256(data).digest()
            finally:
                if close_after:
                    _close(fd)

    def seal(
        self,
        handle: QuarantineHandle,
        *,
        expected_size: int,
        expected_sha256: bytes,
        incremental_sha256: bytes | None = None,
    ) -> bytes:
        """fsync, re-hash the whole file, verify identity, and close the writer."""
        with self._lock:
            if handle.fd is None or handle.sealed or handle.discarded or handle.published:
                raise QuarantineError("invalid_transfer_state", "The quarantine file cannot be sealed.")
            if not isinstance(expected_sha256, (bytes, bytearray)) or len(expected_sha256) != 32:
                raise QuarantineError("invalid_sha256", "The expected sha256 is invalid.")
            _fsync(handle.fd)
            try:
                st = os.fstat(handle.fd)
            except OSError as exc:
                raise _map_os_error(exc) from exc
            _validate_regular_file(st, identity=handle.identity, expected_uid=_current_uid())
            if st.st_size != expected_size:
                raise QuarantineError(
                    "integrity_failed",
                    "The received size does not match the declared size.",
                )
            # Close the write-only FD and reopen for reading.
            _close(handle.fd)
            handle.fd = None
            root = self._root_for_tier(handle.tier)
            read_fd = None
            try:
                read_fd = os.open(
                    handle.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=root.fd,
                )
                rd_st = os.fstat(read_fd)
                if (rd_st.st_dev, rd_st.st_ino) != (handle.identity.dev, handle.identity.ino):
                    raise QuarantineError("file_replaced", "The quarantine file was replaced.")
                total, digest = _hash_descriptor(read_fd, expected_size)
                if total != expected_size:
                    raise QuarantineError(
                        "integrity_failed",
                        "The received byte count does not match the declared size.",
                    )
                if incremental_sha256 is not None and bytes(incremental_sha256) != digest:
                    raise QuarantineError(
                        "integrity_failed",
                        "The incremental digest disagrees with the on-disk file.",
                    )
                if digest != bytes(expected_sha256):
                    raise QuarantineError(
                        "integrity_failed",
                        "The whole-file SHA-256 does not match the authenticated metadata.",
                    )
                mnt_id = _get_mount_id(read_fd)
                if mnt_id is None or mnt_id != handle.identity.mnt_id:
                    raise QuarantineError(
                        "file_replaced", "The quarantine file mount identity changed."
                    )
                handle.sealed = True
                return digest
            finally:
                _close(read_fd)

    def _open_handle_locked(self, handle: QuarantineHandle, *, writable: bool) -> int:
        root = self._root_for_tier(handle.tier)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        if writable:
            flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = None
        try:
            fd = os.open(handle.name, flags, dir_fd=root.fd)
            st = os.fstat(fd)
            _validate_regular_file(
                st,
                identity=handle.identity,
                expected_uid=_current_uid(),
                expected_mode=0o600,
            )
            if st.st_size != handle.size:
                raise QuarantineError(
                    "file_modified", "The quarantine file size changed after verification."
                )
            mnt_id = _get_mount_id(fd)
            if mnt_id is None or mnt_id != handle.identity.mnt_id:
                raise QuarantineError("file_replaced", "The quarantine mount identity changed.")
            return fd
        except Exception:
            _close(fd)
            raise

    def open_verified(self, handle: QuarantineHandle) -> int:
        with self._lock:
            return self._open_handle_locked(handle, writable=False)

    def discard(self, handle: QuarantineHandle | None) -> None:
        """Close and unlink one quarantine file; never touch unrelated files."""
        if handle is None or handle.discarded or handle.published:
            return
        with self._lock:
            _close(handle.fd)
            handle.fd = None
            root = self._roots.get(handle.tier)
            if root is not None:
                try:
                    st = os.stat(handle.name, dir_fd=root.fd, follow_symlinks=False)
                except FileNotFoundError:
                    st = None
                except OSError:
                    st = None
                if st is not None:
                    if (
                        stat.S_ISREG(st.st_mode)
                        and st.st_uid == _current_uid()
                        and (st.st_dev, st.st_ino) == (handle.identity.dev, handle.identity.ino)
                    ):
                        try:
                            os.unlink(handle.name, dir_fd=root.fd)
                        except OSError:
                            pass
            handle.discarded = True

    def cleanup_abandoned(self) -> int:
        with self._lock:
            return self._cleanup_abandoned_locked()

    def _cleanup_abandoned_locked(self) -> int:
        removed = 0
        uid = _current_uid()
        for root in self._roots.values():
            try:
                names = os.listdir(root.fd)
            except OSError:
                continue
            for name in names:
                if not (
                    name.startswith(QUARANTINE_FILE_PREFIX)
                    and name.endswith(QUARANTINE_FILE_SUFFIX)
                ):
                    continue
                try:
                    st = os.stat(name, dir_fd=root.fd, follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
                    continue
                try:
                    os.unlink(name, dir_fd=root.fd)
                    removed += 1
                except OSError:
                    continue
        return removed

    # ── Publication ────────────────────────────────────────────────────────

    def resolve_downloads_path(self) -> Path:
        if self._downloads_override is not None:
            candidate = self._downloads_override
        else:
            env_value = os.environ.get("XDG_DOWNLOAD_DIR")
            candidate = Path(env_value) if env_value and os.path.isabs(env_value) else None
            if candidate is None:
                candidate = _read_user_dirs_download()
            if candidate is None:
                candidate = Path.home() / "Downloads"
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise QuarantineError(
                "downloads_unavailable", "The Downloads directory could not be resolved."
            ) from exc
        if not resolved.is_dir():
            raise QuarantineError("downloads_unavailable", "Downloads is not a directory.")
        return resolved

    def _same_filesystem(
        self,
        source_st: os.stat_result,
        source_mnt_id: int,
        destination_st: os.stat_result,
        destination_mnt_id: int,
    ) -> bool:
        return (
            source_st.st_dev == destination_st.st_dev
            and source_mnt_id == destination_mnt_id
        )

    def _create_publication_temp(self, downloads_fd: int) -> tuple[str, int]:
        for _ in range(MAX_QUARANTINE_FILE_ATTEMPTS):
            name = ".kivo-publish-" + secrets.token_hex(16) + ".tmp"
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                    dir_fd=downloads_fd,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise _map_os_error(exc, code="publication_failed") from exc
            try:
                os.fchmod(fd, 0o600)
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != _current_uid():
                    raise QuarantineError(
                        "publication_failed", "The publication temporary is unsafe."
                    )
                return name, fd
            except Exception:
                _close(fd)
                try:
                    os.unlink(name, dir_fd=downloads_fd)
                except OSError:
                    pass
                raise
        raise QuarantineError(
            "temp_name_exhausted", "A unique publication temporary could not be created."
        )

    def _verify_destination(
        self,
        downloads_fd: int,
        name: str,
        *,
        expected_identity: DirectoryIdentity,
        expected_size: int,
        expected_sha256: bytes,
        rehash: bool,
    ) -> None:
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=downloads_fd,
            )
        except OSError as exc:
            raise _map_os_error(exc, code="publication_verification_failed") from exc
        try:
            st = os.fstat(fd)
            _validate_regular_file(
                st,
                identity=expected_identity,
                expected_uid=_current_uid(),
                expected_mode=0o600,
            )
            if st.st_size != expected_size:
                raise QuarantineError(
                    "publication_verification_failed",
                    "The published file size does not match the verified size.",
                )
            if rehash:
                total, digest = _hash_descriptor(fd, expected_size)
                if total != expected_size or digest != expected_sha256:
                    raise QuarantineError(
                        "publication_verification_failed",
                        "The published file digest does not match the verified digest.",
                    )
        finally:
            _close(fd)

    def publish(
        self,
        handle: QuarantineHandle,
        display_name: str,
        *,
        expected_sha256: bytes,
    ) -> PublishedFile:
        """Publish a sealed, verified quarantine file into Downloads.

        The caller is responsible for the final explicit desktop acceptance.
        This method never overwrites an existing destination and never exposes
        the chosen path outside the returned basename.
        """
        safe_name = validate_display_filename(display_name)
        if not isinstance(expected_sha256, (bytes, bytearray)) or len(expected_sha256) != 32:
            raise QuarantineError("invalid_sha256", "The expected sha256 is invalid.")
        if not handle.sealed or handle.fd is not None or handle.published or handle.discarded:
            raise QuarantineError("invalid_transfer_state", "The quarantine file is not publishable.")

        with self._lock:
            downloads_path = self.resolve_downloads_path()
            downloads_fd, downloads_identity = _open_existing_directory(
                downloads_path, private=False
            )
            source_fd = None
            try:
                source_fd = self._open_handle_locked(handle, writable=False)
                st = os.fstat(source_fd)
                _validate_regular_file(
                    st, identity=handle.identity, expected_uid=_current_uid()
                )
                if st.st_size != handle.size:
                    raise QuarantineError(
                        "file_modified", "The quarantine file changed before publication."
                    )
                mnt_id = _get_mount_id(source_fd)
                if mnt_id is None:
                    raise QuarantineError(
                        "mount_identity_unavailable",
                        "The quarantine mount identity could not be verified.",
                    )

                if self._same_filesystem(
                    st, mnt_id, os.fstat(downloads_fd), downloads_identity.mnt_id
                ):
                    published = self._publish_same_filesystem_locked(
                        handle,
                        source_fd,
                        display_name=safe_name,
                        downloads_fd=downloads_fd,
                        expected_sha256=bytes(expected_sha256),
                    )
                else:
                    published = self._publish_cross_filesystem_locked(
                        handle,
                        source_fd,
                        display_name=safe_name,
                        downloads_fd=downloads_fd,
                        downloads_identity=downloads_identity,
                    )
                handle.published = True
                return published
            finally:
                _close(source_fd)
                _close(downloads_fd)

    def _publish_same_filesystem_locked(
        self,
        handle: QuarantineHandle,
        source_fd: int,
        *,
        display_name: str,
        downloads_fd: int,
        expected_sha256: bytes,
    ) -> PublishedFile:
        source_st = os.fstat(source_fd)
        source_identity = DirectoryIdentity(
            dev=source_st.st_dev,
            ino=source_st.st_ino,
            mnt_id=handle.identity.mnt_id,
        )
        for candidate in _candidate_names(display_name):
            try:
                _renameat2_noreplace(handle.root.fd, handle.name, downloads_fd, candidate)
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    continue
                if exc.errno == errno.EXDEV:
                    raise QuarantineError(
                        "cross_device_boundary",
                        "The publication source and destination are on different filesystems.",
                    )
                raise _map_os_error(exc, code="publication_failed") from exc
            except QuarantineError:
                raise

            try:
                _fsync(downloads_fd)
            except QuarantineError:
                # The file is already published atomically.  Report success but
                # preserve the destination; durability may be weaker.
                pass
            self._verify_destination(
                downloads_fd,
                candidate,
                expected_identity=source_identity,
                expected_size=handle.size,
                expected_sha256=expected_sha256,
                rehash=False,
            )
            return PublishedFile(
                name=candidate,
                size=handle.size,
                sha256=expected_sha256,
                cross_filesystem=False,
            )
        raise QuarantineError(
            "publication_collision_exhausted",
            "A unique destination filename could not be found.",
        )

    def _publish_cross_filesystem_locked(
        self,
        handle: QuarantineHandle,
        source_fd: int,
        *,
        display_name: str,
        downloads_fd: int,
        downloads_identity: DirectoryIdentity,
    ) -> PublishedFile:
        temp_name, temp_fd = self._create_publication_temp(downloads_fd)
        try:
            try:
                os.lseek(source_fd, 0, os.SEEK_SET)
            except OSError as exc:
                raise _map_os_error(exc, code="publication_failed") from exc
            digest = hashlib.sha256()
            total = 0
            while True:
                try:
                    chunk = os.read(source_fd, COPY_CHUNK_BYTES)
                except OSError as exc:
                    raise _map_os_error(exc, code="publication_failed") from exc
                if not chunk:
                    break
                total += len(chunk)
                if total > handle.size:
                    raise QuarantineError(
                        "publication_failed",
                        "The quarantine source exceeded its verified size.",
                    )
                digest.update(chunk)
                _write_all(temp_fd, chunk)
            if total != handle.size:
                raise QuarantineError(
                    "publication_failed",
                    "The quarantine source ended before its verified size.",
                )
            copied_sha256 = digest.digest()
            _fsync(temp_fd)
            temp_st = os.fstat(temp_fd)
            _validate_regular_file(
                temp_st, expected_uid=_current_uid(), expected_mode=0o600
            )
            if temp_st.st_size != handle.size:
                raise QuarantineError(
                    "publication_failed", "The publication copy has an unexpected size."
                )
            temp_identity = DirectoryIdentity(
                dev=temp_st.st_dev,
                ino=temp_st.st_ino,
                mnt_id=downloads_identity.mnt_id,
            )
            _close(temp_fd)
            temp_fd = None

            for candidate in _candidate_names(display_name):
                try:
                    _renameat2_noreplace(downloads_fd, temp_name, downloads_fd, candidate)
                except OSError as exc:
                    if exc.errno == errno.EEXIST:
                        continue
                    raise _map_os_error(exc, code="publication_failed") from exc
                except QuarantineError:
                    raise

                try:
                    _fsync(downloads_fd)
                except QuarantineError:
                    pass
                self._verify_destination(
                    downloads_fd,
                    candidate,
                    expected_identity=temp_identity,
                    expected_size=handle.size,
                    expected_sha256=copied_sha256,
                    rehash=True,
                )
                # The verified destination is durable.  Remove the quarantine
                # source only after publication succeeded.
                self._unlink_source_locked(handle)
                return PublishedFile(
                    name=candidate,
                    size=handle.size,
                    sha256=copied_sha256,
                    cross_filesystem=True,
                )
            raise QuarantineError(
                "publication_collision_exhausted",
                "A unique destination filename could not be found.",
            )
        except Exception:
            if temp_fd is not None:
                _close(temp_fd)
            try:
                os.unlink(temp_name, dir_fd=downloads_fd)
            except OSError:
                pass
            raise

    def _unlink_source_locked(self, handle: QuarantineHandle) -> None:
        root = self._root_for_tier(handle.tier)
        try:
            st = os.stat(handle.name, dir_fd=root.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            return
        if (
            stat.S_ISREG(st.st_mode)
            and st.st_uid == _current_uid()
            and (st.st_dev, st.st_ino) == (handle.identity.dev, handle.identity.ino)
        ):
            try:
                os.unlink(handle.name, dir_fd=root.fd)
            except OSError:
                pass


__all__ = [
    "QuarantineError",
    "QuarantineManager",
    "QuarantineRoot",
    "QuarantineHandle",
    "Reservation",
    "PublishedFile",
    "DirectoryIdentity",
    "QUARANTINE_TIER_RUNTIME",
    "QUARANTINE_TIER_CACHE",
    "QUARANTINE_TIER_SPLIT_BYTES",
    "TMPFS_SAFETY_MARGIN_BYTES",
    "DISK_SAFETY_MARGIN_BYTES",
]
