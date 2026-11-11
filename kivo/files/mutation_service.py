"""Atomic, approval-bound creation of one new UTF-8 text file.

Stage 11.3.1b implements the service and the tool/preflight/handler pair, but
does **not** register the tool or expose it to the conversational model.  The
service is directly testable and can only be reached by code that explicitly
constructs it with a mutation-eligible workspace allowlist.

Security model
--------------
* The workspace identifier is resolved through the existing explicit
  ``workspaces.json`` configuration.  There is no implicit root and no path
  fallback.
* A workspace must be listed in a code-owned mutation allowlist in addition to
  being configured.  Stage 11.3.1c owns the opt-in user configuration.
* The parent directory is traversed one descriptor at a time with
  ``O_NOFOLLOW | O_DIRECTORY``.  Absolute paths, ``..``, empty components,
  backslashes, control characters, bidi controls, and sensitive components are
  rejected before any descriptor is opened.
* The root and every descendant parent directory must be owned by the current
  user and must not be group- or world-writable.
* Device and Linux mount identities are verified at every directory.  Failure
  to establish either identity fails closed.
* The final target must not exist.  An existing file, directory, symlink,
  FIFO, socket, or device is never overwritten.
* The file is written to a cryptographically named temporary file in the same
  directory, fsynced, and installed with ``renameat2(RENAME_NOREPLACE)``.
  There is deliberately no fallback to ``os.rename``/``os.replace``.
* The installed file is reopened descriptor-relatively and verified by device,
  inode, mount ID, owner, mode, link count, size, and content hash.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import platform
import re
import secrets
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from kivo.agent.approval_context import (
    ApprovalDisplay,
    MutationBinding,
    MutationPreflight,
)
from kivo.agent.mutation_lifecycle import (
    DURABILITY_FAILED,
    DURABILITY_SYNCED,
    MutationLifecycle,
)
from kivo.files.service import (
    INTERNAL_TEMP_PREFIX,
    MAX_COMPONENT_BYTES,
    MAX_RELATIVE_PATH_CHARS,
    READ_DENIED_COMPONENTS,
    _get_mount_id,
    _is_sensitive_relative,
)
from kivo.files.workspace import (
    WORKSPACE_ID_PATTERN,
    WorkspaceConfigError,
    WorkspaceRoot,
    load_workspace_config,
)

MAX_CREATE_CONTENT_BYTES = 8_192
MAX_MUTATION_TEMP_ATTEMPTS = 8
MUTATION_WRITE_CHUNK_BYTES = 4_096

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

_BIDI_CONTROLS = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A))
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_RENAMEAT2_UNAVAILABLE_ERRNOS = frozenset(
    {
        errno.ENOSYS,
        errno.EINVAL,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    }
)


class FileMutationError(Exception):
    """A safe, structured refusal or failure from the mutation service.

    ``committed`` is true only when the final path is known to have been
    installed.  A caller must never report a committed failure as if no file
    exists.
    """

    def __init__(self, code: str, message: str, *, committed: bool = False):
        self.code = code
        self.message = message
        self.committed = bool(committed)
        super().__init__(message)


@dataclass(frozen=True)
class _DirectoryIdentity:
    st: os.stat_result
    mnt_id: int

    @property
    def token(self) -> str:
        return (
            f"dev:{self.st.st_dev}:ino:{self.st.st_ino}:mnt:{self.mnt_id}"
        )


def _current_uid() -> int:
    """Patchable owner check used by direct tests."""
    return os.getuid()


def _close(fd: Optional[int]) -> None:
    if isinstance(fd, int) and fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


def _cancelled(event: Optional[threading.Event]) -> bool:
    if event is None:
        return False
    try:
        return bool(event.is_set())
    except Exception:
        # A broken cancellation primitive must fail closed for a mutation.
        return True


def _map_os_error(
    exc: OSError,
    *,
    code: Optional[str] = None,
    message: Optional[str] = None,
) -> FileMutationError:
    err = getattr(exc, "errno", None)
    if code is None:
        if err == errno.ENOENT:
            code = "path_not_found"
            message = "The parent directory does not exist."
        elif err == errno.ENOTDIR:
            code = "path_component_not_directory"
            message = "A path component is not a directory."
        elif err in (errno.EACCES, errno.EPERM):
            code = "permission_denied"
            message = "The filesystem denied access to the requested path."
        elif err == errno.ELOOP:
            code = "symlink_not_allowed"
            message = "Symbolic links are not permitted inside a mutable workspace."
        elif err in (errno.ENAMETOOLONG, errno.EINVAL):
            code = "invalid_path"
            message = "The requested path is not valid."
        elif err in (errno.ENOSPC, errno.EDQUOT):
            code = "disk_full"
            message = "The filesystem has no space left for the requested file."
        elif err == errno.EROFS:
            code = "read_only_filesystem"
            message = "The workspace filesystem is read-only."
        elif err == errno.EXDEV:
            code = "cross_device_boundary"
            message = "Crossing filesystem mount boundaries is not permitted."
        else:
            code = "filesystem_error"
            message = "The filesystem operation could not be completed."
    return FileMutationError(code, message or "The filesystem operation failed.")


def _validate_relative_path(relative_path: object) -> tuple[str, ...]:
    """Strictly validate a target path; never canonicalize it silently."""
    if not isinstance(relative_path, str):
        raise FileMutationError(
            "invalid_path", "The relative path must be a string."
        )
    if len(relative_path) > MAX_RELATIVE_PATH_CHARS:
        raise FileMutationError(
            "invalid_path",
            f"The relative path must be at most {MAX_RELATIVE_PATH_CHARS} characters.",
        )
    if relative_path in ("", "."):
        raise FileMutationError(
            "invalid_path", "A relative file path inside the workspace is required."
        )
    if relative_path.startswith("~"):
        raise FileMutationError(
            "invalid_path",
            "Home-directory expansion is not allowed; use a workspace-relative path.",
        )
    if "\x00" in relative_path or any(
        ord(ch) < 32 or ord(ch) == 127 or 0x7F <= ord(ch) <= 0x9F
        for ch in relative_path
    ):
        raise FileMutationError(
            "invalid_path", "The relative path contains control characters."
        )
    if any(ord(ch) in _BIDI_CONTROLS for ch in relative_path):
        raise FileMutationError(
            "invalid_path",
            "The relative path contains bidirectional control characters.",
        )
    if "\\" in relative_path:
        raise FileMutationError(
            "invalid_path", "Backslashes are not valid path separators on Linux."
        )
    if (
        os.path.isabs(relative_path)
        or relative_path.startswith("/")
        or _DRIVE_PATH.match(relative_path)
    ):
        raise FileMutationError(
            "invalid_path",
            "Absolute paths are not allowed; use a workspace-relative path.",
        )

    components = relative_path.split("/")
    for component in components:
        if component in ("", ".", ".."):
            raise FileMutationError(
                "invalid_path",
                "Malformed path component: empty, '.', and '..' components are not allowed.",
            )
        if len(component.encode("utf-8")) > MAX_COMPONENT_BYTES:
            raise FileMutationError(
                "invalid_path", "A path component is too long."
            )
    return tuple(components)


def _validate_content(content: object) -> bytes:
    if not isinstance(content, str):
        raise FileMutationError(
            "invalid_content", "File content must be a string."
        )
    if "\x00" in content:
        raise FileMutationError(
            "nul_byte", "File content must not contain NUL bytes."
        )
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FileMutationError(
            "invalid_utf8",
            "File content must be valid UTF-8 and must not contain lone surrogates.",
        ) from exc
    if len(encoded) > MAX_CREATE_CONTENT_BYTES:
        raise FileMutationError(
            "content_too_large",
            f"File content is {len(encoded)} bytes; the maximum is "
            f"{MAX_CREATE_CONTENT_BYTES} bytes.",
        )
    return encoded


def _require_safe_relative(components: tuple[str, ...]) -> None:
    if _is_sensitive_relative(
        components, extra_components=READ_DENIED_COMPONENTS
    ):
        raise FileMutationError(
            "sensitive_path",
            "That path is blocked by KIVO's sensitive-file policy.",
        )


def _display_path(relative_path: str) -> str:
    """Bound the path shown in an approval card without leaking content."""
    if len(relative_path) <= 240:
        return relative_path
    return "..." + relative_path[-237:]


def _approval_display(
    workspace_id: str,
    relative_path: str,
    byte_count: int,
    content: str,
) -> ApprovalDisplay:
    return ApprovalDisplay(
        title="Create text file",
        summary=(
            f"Create '{_display_path(relative_path)}' in workspace "
            f"'{workspace_id}' ({byte_count} bytes)."
        ),
        preview=content,
        notice=(
            "This creates a new file. Existing files, directories, symlinks, "
            "and special files are never overwritten; creation fails if the "
            "destination already exists."
        ),
    )


def _open_mutation_root(root: WorkspaceRoot) -> tuple[int, _DirectoryIdentity]:
    try:
        fd = os.open(
            root.path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise _map_os_error(exc) from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise FileMutationError(
                "not_a_directory", "The workspace root is not a directory."
            )
        if st.st_uid != _current_uid():
            raise FileMutationError(
                "unsafe_owner",
                "A mutable workspace root must be owned by the current user.",
            )
        if st.st_mode & 0o022:
            raise FileMutationError(
                "unsafe_permissions",
                "A mutable workspace root must not be group- or world-writable.",
            )
        mnt_id = _get_mount_id(fd)
        if mnt_id is None:
            raise FileMutationError(
                "mount_identity_unavailable",
                "Filesystem mount identity could not be verified for the workspace root.",
            )
        return fd, _DirectoryIdentity(st=st, mnt_id=mnt_id)
    except Exception:
        _close(fd)
        raise


def _verify_mutation_directory(
    fd: int,
    st: os.stat_result,
    root_identity: _DirectoryIdentity,
) -> _DirectoryIdentity:
    if not stat.S_ISDIR(st.st_mode):
        raise FileMutationError(
            "path_component_not_directory", "A path component is not a directory."
        )
    if st.st_dev != root_identity.st.st_dev:
        raise FileMutationError(
            "cross_device_boundary",
            "Crossing filesystem mount boundaries is not permitted inside a mutable workspace.",
        )
    mnt_id = _get_mount_id(fd)
    if mnt_id is None:
        raise FileMutationError(
            "mount_identity_unavailable",
            "Filesystem mount identity could not be verified for a mutation directory.",
        )
    if mnt_id != root_identity.mnt_id:
        raise FileMutationError(
            "cross_device_boundary",
            "Crossing filesystem mount boundaries is not permitted inside a mutable workspace.",
        )
    if st.st_uid != _current_uid():
        raise FileMutationError(
            "unsafe_owner",
            "Every mutable parent directory must be owned by the current user.",
        )
    if st.st_mode & 0o022:
        raise FileMutationError(
            "unsafe_permissions",
            "Mutable parent directories must not be group- or world-writable.",
        )
    return _DirectoryIdentity(st=st, mnt_id=mnt_id)


def _open_parent_directory(
    root_fd: int,
    components: tuple[str, ...],
    root_identity: _DirectoryIdentity,
) -> tuple[int, _DirectoryIdentity]:
    try:
        fd = os.dup(root_fd)
    except OSError as exc:
        raise _map_os_error(exc) from exc

    try:
        current_identity = root_identity
        for component in components:
            try:
                before = os.stat(
                    component, dir_fd=fd, follow_symlinks=False
                )
            except OSError as exc:
                raise _map_os_error(exc) from exc

            if stat.S_ISLNK(before.st_mode):
                raise FileMutationError(
                    "symlink_not_allowed",
                    "Symbolic links are not permitted inside a mutable workspace.",
                )
            if not stat.S_ISDIR(before.st_mode):
                raise FileMutationError(
                    "path_component_not_directory",
                    "A path component is not a directory.",
                )

            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except OSError as exc:
                raise _map_os_error(exc) from exc

            try:
                after = os.fstat(child_fd)
                if (after.st_dev, after.st_ino) != (
                    before.st_dev,
                    before.st_ino,
                ):
                    raise FileMutationError(
                        "changed_during_access",
                        "A directory changed during validation; access was refused.",
                    )
                current_identity = _verify_mutation_directory(
                    child_fd, after, root_identity
                )
            except Exception:
                _close(child_fd)
                raise

            _close(fd)
            fd = child_fd
        return fd, current_identity
    except Exception:
        _close(fd)
        raise


def _require_target_absent(parent_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _map_os_error(exc) from exc
    raise FileMutationError(
        "target_exists",
        "The target path already exists; KIVO never overwrites an existing "
        "file, directory, symlink, or special file.",
    )


def _create_temporary_file(parent_fd: int) -> tuple[str, int]:
    for _ in range(MAX_MUTATION_TEMP_ATTEMPTS):
        temp_name = INTERNAL_TEMP_PREFIX + secrets.token_hex(16)
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise _map_os_error(exc) from exc
        return temp_name, fd
    raise FileMutationError(
        "temp_name_exhausted",
        "A unique temporary filename could not be generated; creation was refused.",
    )


def _verify_temporary_file(
    fd: int,
    root_identity: _DirectoryIdentity,
) -> os.stat_result:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise FileMutationError(
            "unsupported_file_type",
            "The temporary path is not a regular file.",
        )
    if st.st_uid != _current_uid():
        raise FileMutationError(
            "unsafe_owner",
            "The temporary file is not owned by the current user.",
        )
    if st.st_nlink != 1:
        raise FileMutationError(
            "unsafe_link_count",
            "The temporary file must have exactly one hard link.",
        )
    if st.st_dev != root_identity.st.st_dev:
        raise FileMutationError(
            "cross_device_boundary",
            "The temporary file was created on an unexpected device.",
        )
    mnt_id = _get_mount_id(fd)
    if mnt_id is None:
        raise FileMutationError(
            "mount_identity_unavailable",
            "Filesystem mount identity could not be verified for the temporary file.",
        )
    if mnt_id != root_identity.mnt_id:
        raise FileMutationError(
            "cross_device_boundary",
            "The temporary file was created on an unexpected mount.",
        )
    try:
        os.fchmod(fd, 0o600)
    except OSError as exc:
        raise _map_os_error(
            exc,
            code="permission_denied",
            message="The temporary file permissions could not be set to 0600.",
        ) from exc
    after = os.fstat(fd)
    if stat.S_IMODE(after.st_mode) != 0o600:
        raise FileMutationError(
            "unsafe_permissions",
            "The temporary file could not be verified with mode 0600.",
        )
    return after


def _write_fd(fd: int, data: bytes) -> int:
    """Small patch point for write-failure tests."""
    return os.write(fd, data)


def _before_install(cancel_event: Optional[threading.Event]) -> None:
    """Test seam immediately before the final pre-install cancellation check."""
    return None


def _fsync_parent(parent_fd: int) -> None:
    """Small patch point for parent-directory durability tests."""
    os.fsync(parent_fd)


def _write_all(
    fd: int,
    data: bytes,
    cancel_event: Optional[threading.Event],
) -> None:
    offset = 0
    total = len(data)
    while offset < total:
        if _cancelled(cancel_event):
            raise FileMutationError(
                "cancelled", "The file creation was cancelled before installation."
            )
        end = min(offset + MUTATION_WRITE_CHUNK_BYTES, total)
        chunk = memoryview(data)[offset:end]
        while chunk:
            if _cancelled(cancel_event):
                raise FileMutationError(
                    "cancelled",
                    "The file creation was cancelled before installation.",
                )
            try:
                written = _write_fd(fd, bytes(chunk))
            except OSError as exc:
                raise _map_os_error(exc) from exc
            if written <= 0:
                raise FileMutationError(
                    "write_failed",
                    "The file content could not be written completely.",
                )
            chunk = chunk[written:]
        offset = end


def _unlink_temporary(parent_fd: int, temp_name: str) -> None:
    try:
        os.unlink(temp_name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError:
        # Cleanup is best effort; the final path is never removed.
        pass


def _read_limited(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max(0, int(limit))
    while remaining > 0:
        try:
            chunk = os.read(fd, min(65_536, remaining))
        except OSError as exc:
            raise _map_os_error(exc) from exc
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _verify_installed_file(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    data: bytes,
    root_identity: _DirectoryIdentity,
) -> None:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise _map_os_error(exc) from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FileMutationError(
                "verification_failed",
                "The created path is not a regular file.",
                committed=True,
            )
        if (st.st_dev, st.st_ino) != (expected.st_dev, expected.st_ino):
            raise FileMutationError(
                "verification_failed",
                "The created file identity does not match the temporary file.",
                committed=True,
            )
        if st.st_dev != root_identity.st.st_dev:
            raise FileMutationError(
                "verification_failed",
                "The created file is on an unexpected device.",
                committed=True,
            )
        mnt_id = _get_mount_id(fd)
        if mnt_id is None:
            raise FileMutationError(
                "verification_failed",
                "The created file's mount identity could not be verified.",
                committed=True,
            )
        if mnt_id != root_identity.mnt_id:
            raise FileMutationError(
                "verification_failed",
                "The created file is on an unexpected mount.",
                committed=True,
            )
        if st.st_uid != _current_uid():
            raise FileMutationError(
                "verification_failed",
                "The created file is not owned by the current user.",
                committed=True,
            )
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise FileMutationError(
                "verification_failed",
                "The created file does not have mode 0600.",
                committed=True,
            )
        if st.st_nlink != 1:
            raise FileMutationError(
                "verification_failed",
                "The created file does not have exactly one hard link.",
                committed=True,
            )
        if st.st_size != len(data):
            raise FileMutationError(
                "verification_failed",
                "The created file size does not match the approved content.",
                committed=True,
            )
        raw = _read_limited(fd, len(data) + 1)
        if raw != data:
            raise FileMutationError(
                "verification_failed",
                "The created file content does not match the approved content.",
                committed=True,
            )
    finally:
        _close(fd)


def _renameat2_noreplace(
    old_dir_fd: int,
    old_name: str,
    new_dir_fd: int,
    new_name: str,
) -> None:
    """Install with renameat2(RENAME_NOREPLACE); fail closed if unavailable."""
    func = _load_renameat2()
    if func is None:
        raise FileMutationError(
            "renameat2_unavailable",
            "Atomic no-overwrite installation is unavailable on this system; "
            "file creation was refused.",
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
            raise FileMutationError(
                "renameat2_unavailable",
                "Atomic no-overwrite installation is unavailable for this "
                "filesystem; file creation was refused.",
            ) from OSError(err, os.strerror(err))
        raise OSError(err, os.strerror(err))


_RENAMEAT2_FUNC: Any = None
_RENAMEAT2_LOADED = False


def _load_renameat2() -> Any:
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
                ctypes.c_long(syscall_number),
                ctypes.c_int(old_dir_fd),
                ctypes.c_char_p(old_name),
                ctypes.c_int(new_dir_fd),
                ctypes.c_char_p(new_name),
                ctypes.c_uint(flags),
            )
        )

    _RENAMEAT2_FUNC = _syscall_renameat2
    return _RENAMEAT2_FUNC


class FileMutationService:
    """Create one new text file inside an explicitly mutation-approved root.

    Eligibility always requires the user's ``workspaces.json`` entry to contain
    ``"mutation_enabled": true``.  ``mutation_workspace_ids`` is an optional
    additional code-owned restriction; when omitted, the configuration alone
    decides.  There is no way to enable mutation by talking to the model or by
    supplying a workspace path in tool arguments.
    """

    def __init__(
        self,
        config_path: Optional[Path | str] = None,
        *,
        mutation_workspace_ids: Optional[Iterable[str]] = None,
    ):
        self._config_path = (
            Path(config_path).expanduser() if config_path is not None else None
        )
        self._mutation_workspace_ids: Optional[frozenset[str]] = (
            None
            if mutation_workspace_ids is None
            else frozenset(
                item
                for item in mutation_workspace_ids
                if isinstance(item, str)
            )
        )

    def is_mutation_eligible(self, workspace_id: object) -> bool:
        if not isinstance(workspace_id, str):
            return False
        try:
            config = load_workspace_config(self._config_path)
        except WorkspaceConfigError:
            return False
        root = config.get(workspace_id)
        if root is None or not root.mutation_enabled:
            return False
        if (
            self._mutation_workspace_ids is not None
            and workspace_id not in self._mutation_workspace_ids
        ):
            return False
        return True

    # ── Preflight ──────────────────────────────────────────────────────────

    def preflight(
        self,
        workspace_id: object,
        relative_path: object,
        content: object,
    ) -> MutationPreflight:
        """Validate arguments and target state without creating anything."""
        data = _validate_content(content)
        components = _validate_relative_path(relative_path)
        _require_safe_relative(components)
        canonical_path = "/".join(components)
        digest = hashlib.sha256(data).hexdigest()

        root = self._resolve_root(workspace_id)
        root_fd, root_identity = _open_mutation_root(root)
        try:
            parent_fd, parent_identity = _open_parent_directory(
                root_fd, components[:-1], root_identity
            )
            try:
                _require_target_absent(parent_fd, components[-1])
            finally:
                _close(parent_fd)
        finally:
            _close(root_fd)

        return MutationPreflight(
            operation_kind="create_text_file",
            display=_approval_display(
                str(workspace_id), canonical_path, len(data), content
            ),
            binding=MutationBinding(
                workspace_id=str(workspace_id),
                relative_path=canonical_path,
                content_digest=digest,
                byte_count=len(data),
                target_state="absent",
                root_identity=root_identity.token,
                parent_identity=parent_identity.token,
            ),
        )

    # ── Atomic creation ────────────────────────────────────────────────────

    def create_text_file(
        self,
        workspace_id: object,
        relative_path: object,
        content: object,
        approved_binding: MutationBinding,
        *,
        cancel_event: Optional[threading.Event] = None,
        cancellation_event: Optional[threading.Event] = None,
        lifecycle: Optional[MutationLifecycle] = None,
    ) -> dict[str, Any]:
        """Create the approved file atomically and return a safe result.

        The service never returns content and never returns an absolute
        workspace path.  Failures before installation raise with
        ``committed=False``; a verified installation returns ``ok: True``.

        When a ``lifecycle`` is supplied, it is updated at the precise
        filesystem boundaries so an executor timeout or cancellation can never
        mistake a committed file for a failed operation.
        """
        event = cancel_event if cancel_event is not None else cancellation_event
        data = _validate_content(content)
        components = _validate_relative_path(relative_path)
        _require_safe_relative(components)
        canonical_path = "/".join(components)
        digest = hashlib.sha256(data).hexdigest()

        if not isinstance(approved_binding, MutationBinding):
            raise FileMutationError(
                "invalid_binding", "A valid approved mutation binding is required."
            )
        if (
            approved_binding.workspace_id != workspace_id
            or approved_binding.relative_path != canonical_path
            or approved_binding.content_digest != digest
            or approved_binding.byte_count != len(data)
            or approved_binding.target_state != "absent"
        ):
            raise FileMutationError(
                "approval_binding_mismatch",
                "The approved mutation binding does not match this request.",
            )
        if lifecycle is not None:
            if lifecycle.context.binding != approved_binding:
                raise FileMutationError(
                    "invalid_lifecycle",
                    "The mutation lifecycle does not match the approved binding.",
                )

        if _cancelled(event):
            if lifecycle is not None:
                lifecycle.record_cancelled(committed=False)
            raise FileMutationError(
                "cancelled", "The file creation was cancelled before it started."
            )

        try:
            return self._create_with_lifecycle(
                workspace_id,
                canonical_path,
                data,
                digest,
                approved_binding,
                event,
                lifecycle,
            )
        except FileMutationError as exc:
            if lifecycle is not None:
                lifecycle.record_failure(
                    exc.code, exc.message, committed=exc.committed
                )
            raise
        except Exception:
            if lifecycle is not None:
                lifecycle.record_failure(
                    "internal_error",
                    "The file creation failed safely.",
                    committed=lifecycle.committed,
                )
            raise

    def _create_with_lifecycle(
        self,
        workspace_id: object,
        canonical_path: str,
        data: bytes,
        digest: str,
        approved_binding: MutationBinding,
        event: Optional[threading.Event],
        lifecycle: Optional[MutationLifecycle],
    ) -> dict[str, Any]:
        root = self._resolve_root(workspace_id)
        root_fd, root_identity = _open_mutation_root(root)
        try:
            if root_identity.token != approved_binding.root_identity:
                raise FileMutationError(
                    "state_changed",
                    "The workspace root changed after approval; creation was refused.",
                )
            parent_fd, parent_identity = _open_parent_directory(
                root_fd, tuple(canonical_path.split("/"))[:-1], root_identity
            )
            try:
                if parent_identity.token != approved_binding.parent_identity:
                    raise FileMutationError(
                        "state_changed",
                        "The target parent directory changed after approval; "
                        "creation was refused.",
                    )
                _require_target_absent(
                    parent_fd, canonical_path.split("/")[-1]
                )
                if _cancelled(event):
                    raise FileMutationError(
                        "cancelled",
                        "The file creation was cancelled before installation.",
                    )

                temp_stat, cancelled_after_commit = self._install(
                    parent_fd,
                    canonical_path.split("/")[-1],
                    data,
                    root_identity,
                    event,
                    lifecycle,
                )
            finally:
                _close(parent_fd)
        finally:
            _close(root_fd)

        result = {
            "ok": True,
            "status": "created",
            "workspace_id": workspace_id,
            "relative_path": canonical_path,
            "byte_count": len(data),
            "content_digest": digest,
            "verified": True,
            "committed": True,
            "durability": DURABILITY_SYNCED,
            "mode": "0600",
            "cancelled_after_commit": bool(cancelled_after_commit),
        }
        if lifecycle is not None:
            lifecycle.complete(result)
        return result

    def _install(
        self,
        parent_fd: int,
        name: str,
        data: bytes,
        root_identity: _DirectoryIdentity,
        cancel_event: Optional[threading.Event],
        lifecycle: Optional[MutationLifecycle] = None,
    ) -> tuple[os.stat_result, bool]:
        temp_name: Optional[str] = None
        temp_fd: Optional[int] = None
        installed = False
        cancelled_after_commit = False

        try:
            temp_name, temp_fd = _create_temporary_file(parent_fd)
            temp_stat = _verify_temporary_file(temp_fd, root_identity)

            if _cancelled(cancel_event):
                raise FileMutationError(
                    "cancelled",
                    "The file creation was cancelled before installation.",
                )

            _write_all(temp_fd, data, cancel_event)
            if _cancelled(cancel_event):
                raise FileMutationError(
                    "cancelled",
                    "The file creation was cancelled before installation.",
                )

            try:
                os.fsync(temp_fd)
            except OSError as exc:
                raise _map_os_error(
                    exc,
                    code="fsync_failed",
                    message="The temporary file could not be synchronized; "
                    "installation was refused.",
                ) from exc

            temp_stat = os.fstat(temp_fd)
            if _cancelled(cancel_event):
                raise FileMutationError(
                    "cancelled",
                    "The file creation was cancelled before installation.",
                )

            # Re-verify the temporary file immediately before installation.
            # This catches a hard link, permission change, or directory/mount
            # substitution that occurred while content was being written.
            temp_stat = _verify_temporary_file(temp_fd, root_identity)
            _before_install(cancel_event)
            if _cancelled(cancel_event):
                raise FileMutationError(
                    "cancelled",
                    "The file creation was cancelled before installation.",
                )

            try:
                _renameat2_noreplace(parent_fd, temp_name, parent_fd, name)
            except FileMutationError:
                raise
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    raise FileMutationError(
                        "target_exists",
                        "The target path already exists; KIVO never overwrites "
                        "an existing file, directory, symlink, or special file.",
                    ) from exc
                if exc.errno in _RENAMEAT2_UNAVAILABLE_ERRNOS:
                    raise FileMutationError(
                        "renameat2_unavailable",
                        "Atomic no-overwrite installation is unavailable for "
                        "this filesystem; file creation was refused.",
                    ) from exc
                raise _map_os_error(exc) from exc

            installed = True
            temp_name = None
            # This is the commit boundary. Record it before any further I/O
            # so a timeout, cancellation, or shutdown cannot lose the fact.
            if lifecycle is not None:
                lifecycle.mark_committed()
            if _cancelled(cancel_event):
                cancelled_after_commit = True
                if lifecycle is not None:
                    lifecycle.record_cancelled(committed=True)

            try:
                _fsync_parent(parent_fd)
            except OSError as exc:
                if lifecycle is not None:
                    lifecycle.mark_durability(DURABILITY_FAILED)
                raise _map_os_error(
                    exc,
                    code="fsync_failed",
                    message=(
                        "The file was created, but the parent directory could "
                        "not be synchronized; durability is unverified."
                    ),
                ) from exc
            if lifecycle is not None:
                lifecycle.mark_durability(DURABILITY_SYNCED)

            try:
                _verify_installed_file(
                    parent_fd, name, temp_stat, data, root_identity
                )
            except FileMutationError:
                if lifecycle is not None:
                    lifecycle.mark_verification(False)
                raise
            if lifecycle is not None:
                lifecycle.mark_verification(True)
            return temp_stat, cancelled_after_commit
        except FileMutationError as exc:
            if not installed and temp_name is not None:
                _unlink_temporary(parent_fd, temp_name)
            if installed and not exc.committed:
                raise FileMutationError(
                    exc.code,
                    exc.message,
                    committed=True,
                ) from exc
            raise
        except OSError as exc:
            if not installed and temp_name is not None:
                _unlink_temporary(parent_fd, temp_name)
            mapped = _map_os_error(exc)
            if installed:
                raise FileMutationError(
                    mapped.code, mapped.message, committed=True
                ) from exc
            raise mapped from exc
        finally:
            _close(temp_fd)
            if not installed and temp_name is not None:
                _unlink_temporary(parent_fd, temp_name)

    # ── Workspace resolution ───────────────────────────────────────────────

    def _resolve_root(self, workspace_id: object) -> WorkspaceRoot:
        if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.fullmatch(
            workspace_id
        ):
            raise FileMutationError(
                "invalid_workspace_id",
                "Workspace identifiers must be 1-64 letters, digits, dots, "
                "underscores, or hyphens, and must not be paths.",
            )
        if (
            self._mutation_workspace_ids is not None
            and workspace_id not in self._mutation_workspace_ids
        ):
            raise FileMutationError(
                "workspace_not_mutation_eligible",
                "That workspace is not explicitly approved for mutation.",
            )

        try:
            config = load_workspace_config(self._config_path)
        except WorkspaceConfigError as exc:
            raise FileMutationError(
                "config_error",
                "The workspace configuration is invalid. Fix the workspace "
                "configuration file before using mutation tools.",
            ) from exc

        root = config.get(workspace_id)
        if root is not None:
            if not root.mutation_enabled:
                raise FileMutationError(
                    "workspace_not_mutation_eligible",
                    "That workspace is configured read-only. The user must "
                    "explicitly set \"mutation_enabled\": true for this "
                    "workspace in workspaces.json.",
                )
            return root
        if not config.roots:
            raise FileMutationError(
                "workspace_not_configured",
                "No approved workspace roots are configured. Create "
                "$XDG_CONFIG_HOME/kivo/workspaces.json with an explicit "
                "workspace identifier and absolute directory before using "
                "mutation tools.",
            )
        raise FileMutationError(
            "unknown_workspace",
            f"Workspace '{workspace_id}' is not configured. "
            f"Available workspace IDs: {', '.join(config.ids())}.",
        )


__all__ = [
    "FileMutationError",
    "FileMutationService",
    "MAX_CREATE_CONTENT_BYTES",
    "MAX_MUTATION_TEMP_ATTEMPTS",
    "MUTATION_WRITE_CHUNK_BYTES",
]
