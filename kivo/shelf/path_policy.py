"""URI parsing, validation, and descriptor-relative path authorization for File Shelf."""
from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from kivo.files.service import (
    _get_mount_id,
    _is_sensitive_component,
    _map_os_error,
)
from kivo.files.workspace import WorkspaceConfig, load_workspace_config
from kivo.shelf.models import (
    MAX_URI_BYTES,
    FileIdentity,
)

_BIDI_CONTROL_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BLOCKED_SYSTEM_PREFIXES = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/root",
    "/var/run",
    "/var/log",
)


class ShelfPolicyError(Exception):
    """Raised when a URI or path fails policy validation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_and_validate_file_uri(uri: str) -> str:
    """Validate a local file:// URI and return the decoded filesystem path.

    Enforces strict scheme, host authority, length, encoding, and character checks.
    """
    if not isinstance(uri, str):
        raise ShelfPolicyError("invalid_uri", "URI must be a string")
    if not uri.strip():
        raise ShelfPolicyError("invalid_uri", "URI must not be empty")
    if len(uri.encode("utf-8")) > MAX_URI_BYTES:
        raise ShelfPolicyError("uri_too_long", f"URI exceeds {MAX_URI_BYTES} bytes")
    if "\x00" in uri:
        raise ShelfPolicyError("invalid_uri", "URI contains null bytes")
    if _BIDI_CONTROL_RE.search(uri) or _CONTROL_CHAR_RE.search(uri):
        raise ShelfPolicyError("invalid_uri", "URI contains control or bidi override characters")

    try:
        parsed = urlparse(uri)
    except Exception as exc:
        raise ShelfPolicyError("malformed_uri", f"Malformed URI syntax: {exc}") from exc

    if parsed.scheme != "file":
        raise ShelfPolicyError(
            "unsupported_scheme",
            f"Unsupported URI scheme '{parsed.scheme}'; only local 'file://' is supported",
        )

    # Reject remote network authorities
    if parsed.netloc and parsed.netloc != "localhost":
        raise ShelfPolicyError(
            "remote_authority_rejected",
            f"Remote authority '{parsed.netloc}' is not permitted; only local files are supported",
        )

    raw_path = parsed.path
    if not raw_path:
        raise ShelfPolicyError("invalid_uri", "File URI missing path component")

    # Double-encoding / traversal detection: decode exactly once
    unquoted = unquote(raw_path)
    if "%" in unquoted:
        # Check if secondary unquote alters path traversal
        second_unquote = unquote(unquoted)
        if ".." in second_unquote.split("/") and ".." not in unquoted.split("/"):
            raise ShelfPolicyError("traversal_detected", "Double-encoded path traversal detected")

    # Reject NUL bytes or control characters in decoded path
    if "\x00" in unquoted or _CONTROL_CHAR_RE.search(unquoted) or _BIDI_CONTROL_RE.search(unquoted):
        raise ShelfPolicyError("invalid_uri", "Decoded path contains control characters")

    # Normalize path
    norm_path = os.path.normpath(unquoted)
    if not os.path.isabs(norm_path):
        raise ShelfPolicyError("relative_path_rejected", "File URI must resolve to an absolute path")

    return norm_path


class AuthorizedFileDescriptor:
    """A retained, identity-verified read descriptor for an authorized file.

    The descriptor is opened with ``O_NOFOLLOW`` and is owned by the caller
    until :meth:`close` is invoked.  Bridge downloads use this so that the
    bytes streamed come from exactly the inode that was authorized, even if
    the original path is replaced after authorization.
    """

    def __init__(
        self,
        *,
        fd: int,
        canonical_path: str,
        identity: FileIdentity,
        is_workspace: bool = False,
        workspace_name: str | None = None,
    ):
        self._fd: int | None = fd
        self.canonical_path = canonical_path
        self.identity = identity
        self.is_workspace = is_workspace
        self.workspace_name = workspace_name

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise ValueError("descriptor is closed")
        return self._fd

    @property
    def closed(self) -> bool:
        return self._fd is None

    def close(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "AuthorizedFileDescriptor":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort safety net
        try:
            self.close()
        except Exception:
            pass


class PathAuthorizer:
    """Verifies that staged files are authorized regular files using O_NOFOLLOW traversal."""

    def __init__(self, workspace_config: Optional[WorkspaceConfig] = None):
        self._workspace_config = workspace_config

    def _get_workspaces(self) -> WorkspaceConfig:
        if self._workspace_config is not None:
            return self._workspace_config
        try:
            return load_workspace_config()
        except Exception:
            return WorkspaceConfig.empty()

    def _evaluate_path(self, absolute_path: str) -> tuple[str, bool, str | None]:
        """Apply prefix, sensitivity, and workspace policy to a normalized path."""
        clean_path = os.path.normpath(absolute_path)

        # 1. Block dangerous system prefixes
        for prefix in _BLOCKED_SYSTEM_PREFIXES:
            if clean_path == prefix or clean_path.startswith(prefix + "/"):
                raise ShelfPolicyError(
                    "system_path_denied",
                    f"Access to system directory '{prefix}' is forbidden",
                )

        # 2. Block sensitive file heuristics (.ssh, .gnupg, private keys, socket files)
        path_obj = Path(clean_path)
        for part in path_obj.parts:
            if _is_sensitive_component(part):
                raise ShelfPolicyError(
                    "sensitive_path_denied",
                    f"Access to sensitive path component '{part}' is forbidden",
                )

        # 3. Check if path belongs to a configured workspace root
        workspaces = self._get_workspaces()
        matched_ws_name: str | None = None

        for ws_id in workspaces.ids():
            ws_root = workspaces.get(ws_id)
            if ws_root and ws_root.path:
                try:
                    path_obj.relative_to(ws_root.path)
                    matched_ws_name = ws_id
                    break
                except ValueError:
                    continue

        return clean_path, matched_ws_name is not None, matched_ws_name

    def authorize_and_inspect(
        self, absolute_path: str
    ) -> tuple[str, FileIdentity, bool, str | None]:
        """Verify path authorization and capture filesystem identity without following symlinks.

        Returns:
            (canonical_path, FileIdentity, is_workspace: bool, workspace_name: str | None)

        Raises:
            ShelfPolicyError on authorization failure, symlinks, or non-regular files.
        """
        clean_path, is_workspace, matched_ws_name = self._evaluate_path(absolute_path)
        descriptor = self._open_descriptor(clean_path, is_workspace, matched_ws_name)
        try:
            return (
                descriptor.canonical_path,
                descriptor.identity,
                descriptor.is_workspace,
                descriptor.workspace_name,
            )
        finally:
            descriptor.close()

    def open_authorized_descriptor(self, absolute_path: str) -> AuthorizedFileDescriptor:
        """Revalidate a path and return its retained, read-only descriptor.

        This performs exactly the same policy checks as
        :meth:`authorize_and_inspect`, but the verified descriptor remains open
        and is owned by the returned object.  Callers must close it.
        """
        clean_path, is_workspace, matched_ws_name = self._evaluate_path(absolute_path)
        return self._open_descriptor(clean_path, is_workspace, matched_ws_name)

    def verify_existing_identity(
        self, canonical_path: str, expected_identity: FileIdentity
    ) -> bool:
        """Revalidate that the file at canonical_path still matches expected device and inode."""
        try:
            current_identity, _ = self._safe_descriptor_open(canonical_path)
        except ShelfPolicyError:
            return False

        # Inode and device must match exactly
        if (current_identity.dev, current_identity.ino) != (
            expected_identity.dev,
            expected_identity.ino,
        ):
            return False

        # Must still be a regular file
        if not stat.S_ISREG(current_identity.mode):
            return False

        return True

    def _safe_descriptor_open(self, target_path: str) -> tuple[FileIdentity, str]:
        """Traverse and open target path using O_NOFOLLOW without following symlinks."""
        descriptor = self._open_descriptor(target_path, False, None)
        try:
            return descriptor.identity, descriptor.canonical_path
        finally:
            descriptor.close()

    def _open_descriptor(
        self,
        target_path: str,
        is_workspace: bool,
        workspace_name: str | None,
    ) -> AuthorizedFileDescriptor:
        """Traverse and open target path using O_NOFOLLOW, retaining the file descriptor."""
        path_obj = Path(target_path)
        parts = path_obj.parts

        open_fds: list[int] = []

        try:
            # Start traversal at filesystem root "/"
            try:
                current_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                open_fds.append(current_fd)
            except OSError as exc:
                raise ShelfPolicyError("fs_error", f"Could not open filesystem root: {exc}") from exc

            # Step through directory components with O_NOFOLLOW
            for part in parts[1:-1]:
                if not part or part == ".":
                    continue
                if part == "..":
                    raise ShelfPolicyError("traversal_detected", "Path traversal detected")

                try:
                    # Check before open that it is not a symlink
                    st_before = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                    if stat.S_ISLNK(st_before.st_mode):
                        raise ShelfPolicyError(
                            "symlink_not_allowed",
                            f"Symbolic link encountered in path component '{part}'",
                        )
                    if not stat.S_ISDIR(st_before.st_mode):
                        raise ShelfPolicyError(
                            "not_a_directory",
                            f"Path component '{part}' is not a directory",
                        )

                    next_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        raise ShelfPolicyError("file_not_found", f"Path component '{part}' not found") from exc
                    if exc.errno == errno.EACCES:
                        raise ShelfPolicyError("permission_denied", f"Permission denied for '{part}'") from exc
                    raise ShelfPolicyError("fs_error", str(exc)) from exc

                # Close previous descriptor and track next
                if current_fd in open_fds:
                    open_fds.remove(current_fd)
                try:
                    os.close(current_fd)
                except OSError:
                    pass
                current_fd = next_fd
                open_fds.append(current_fd)

            # Final component: regular file
            target_name = parts[-1]
            try:
                st_before = os.stat(target_name, dir_fd=current_fd, follow_symlinks=False)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    raise ShelfPolicyError("file_not_found", f"File '{target_name}' not found") from exc
                if exc.errno == errno.EACCES:
                    raise ShelfPolicyError("permission_denied", f"Permission denied for '{target_name}'") from exc
                raise ShelfPolicyError("fs_error", str(exc)) from exc

            if stat.S_ISLNK(st_before.st_mode):
                raise ShelfPolicyError(
                    "symlink_not_allowed",
                    f"Symbolic link '{target_name}' is not permitted on the File Shelf",
                )
            if not stat.S_ISREG(st_before.st_mode):
                raise ShelfPolicyError(
                    "unsupported_file_type",
                    f"'{target_name}' is not a regular file (directories, devices, sockets are not supported)",
                )

            # Open final file with O_NOFOLLOW | O_NONBLOCK
            try:
                target_fd = os.open(
                    target_name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=current_fd,
                )
                open_fds.append(target_fd)
            except OSError as exc:
                if exc.errno == errno.EACCES:
                    raise ShelfPolicyError("permission_denied", f"Permission denied for '{target_name}'") from exc
                raise ShelfPolicyError("fs_error", str(exc)) from exc

            st_after = os.fstat(target_fd)
            # Verify identity didn't change between stat and open (TOCTOU protection)
            if (st_after.st_dev, st_after.st_ino) != (st_before.st_dev, st_before.st_ino):
                raise ShelfPolicyError(
                    "changed_during_access",
                    f"File '{target_name}' changed during validation",
                )
            if not stat.S_ISREG(st_after.st_mode):
                raise ShelfPolicyError(
                    "unsupported_file_type",
                    f"'{target_name}' is not a regular file",
                )

            identity = FileIdentity(
                dev=st_after.st_dev,
                ino=st_after.st_ino,
                size=st_after.st_size,
                mtime_ns=st_after.st_mtime_ns,
                mode=st_after.st_mode,
                mnt_id=_get_mount_id(target_fd),
            )
            # Ownership of the final descriptor passes to the caller.
            open_fds.remove(target_fd)
            return AuthorizedFileDescriptor(
                fd=target_fd,
                canonical_path=str(path_obj.resolve()),
                identity=identity,
                is_workspace=is_workspace,
                workspace_name=workspace_name,
            )

        finally:
            for fd in open_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass


__all__ = [
    "AuthorizedFileDescriptor",
    "ShelfPolicyError",
    "parse_and_validate_file_uri",
    "PathAuthorizer",
]
