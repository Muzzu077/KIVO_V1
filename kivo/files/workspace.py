"""Explicit approved-workspace-root configuration for file intelligence.

There is intentionally no implicit root. A workspace is available only when
its identifier and absolute directory are listed in the user's configuration
file:

    $XDG_CONFIG_HOME/kivo/workspaces.json
    {
      "version": 1,
      "workspaces": {
        "my-project": "/absolute/path/to/my-project"
      }
    }

``KIVO_WORKSPACES_CONFIG`` may point at a different file for tests or for a
user-managed configuration. The file itself is not a secret, but it is never
returned to the model; only workspace identifiers are disclosed.
"""
from __future__ import annotations

import errno
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

WORKSPACE_ID_MAX_LENGTH = 64
MAX_CONFIG_BYTES = 64 * 1024
MAX_WORKSPACES = 32
MAX_ROOT_PATH_CHARS = 4096

WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# A configured root may not be a system pseudo-filesystem or a location that
# would make an accidentally broad configuration look like a workspace.
SYSTEM_DENY_ROOTS = (
    Path("/etc"),
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/run"),
    Path("/boot"),
    Path("/root"),
)

# Exact components that are never acceptable as an approved root. A project
# directory whose *name* merely contains "private" or "token" is not rejected;
# the sensitive filename rules apply to paths below the approved root.
SENSITIVE_ROOT_COMPONENTS = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
    }
)
SENSITIVE_ROOT_SEQUENCES = (
    (".config", "kivo"),
    (".local", "share", "kivo"),
)


class WorkspaceConfigError(Exception):
    """The approved-root configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class WorkspaceRoot:
    """One explicitly approved workspace directory."""

    id: str
    path: Path

    @classmethod
    def from_path(cls, workspace_id: str, raw_path: str | Path) -> "WorkspaceRoot":
        """Validate and canonicalize one configured root."""
        if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.fullmatch(
            workspace_id
        ):
            raise WorkspaceConfigError("A workspace identifier is malformed.")

        raw = str(raw_path)
        if not raw or len(raw) > MAX_ROOT_PATH_CHARS:
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' has an invalid root path."
            )
        if "\x00" in raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' has an invalid root path."
            )

        try:
            candidate = Path(raw).expanduser()
        except Exception as exc:
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' has an invalid root path."
            ) from exc

        if not candidate.is_absolute():
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' must use an absolute root path."
            )

        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' root does not exist or cannot be resolved."
            ) from exc

        if not resolved.is_dir():
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' root is not a directory."
            )

        try:
            mode = os.stat(resolved, follow_symlinks=False).st_mode
        except OSError as exc:
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' root cannot be inspected."
            ) from exc
        if not stat.S_ISDIR(mode):
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' root is not a directory."
            )

        home = Path.home().resolve()
        if resolved == home or home.is_relative_to(resolved):
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' may not be the home directory or a parent of it."
            )

        for system_root in SYSTEM_DENY_ROOTS:
            if resolved == system_root or resolved.is_relative_to(system_root):
                raise WorkspaceConfigError(
                    f"Workspace '{workspace_id}' is inside a protected system location."
                )

        lowered = tuple(part.casefold() for part in resolved.parts)
        if any(part in SENSITIVE_ROOT_COMPONENTS for part in lowered):
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' is inside a protected credential location."
            )
        if _contains_sequence(lowered, SENSITIVE_ROOT_SEQUENCES):
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' is inside a protected KIVO configuration location."
            )

        return cls(id=workspace_id, path=resolved)


@dataclass(frozen=True)
class WorkspaceConfig:
    """Immutable set of approved roots, keyed by workspace identifier."""

    roots: Mapping[str, WorkspaceRoot]

    @classmethod
    def empty(cls) -> "WorkspaceConfig":
        return cls(roots=MappingProxyType({}))

    def get(self, workspace_id: str) -> Optional[WorkspaceRoot]:
        return self.roots.get(workspace_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.roots))


def default_config_path() -> Path:
    """Resolve the user's workspace configuration path without creating it."""
    override = os.environ.get("KIVO_WORKSPACES_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()

    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg).expanduser() if xdg and os.path.isabs(xdg) else Path.home() / ".config"
    return base / "kivo" / "workspaces.json"


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Strict JSON object constructor rejecting duplicate keys."""
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise WorkspaceConfigError(f"Duplicate JSON key: '{key}'.")
        out[key] = value
    return out


def _close(fd: int) -> None:
    if isinstance(fd, int) and fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


def _open_config_file(path: Path) -> Optional[int]:
    """Securely open the configuration file descriptor via descriptor-based parent traversal.

    Traverses every parent directory component from '/' using O_NOFOLLOW and
    O_DIRECTORY descriptors, verifying that no parent directory is a symlink,
    that intermediate directories are owned by root or the current user, and
    that the immediate parent directory is owned by the current user and not
    world-writable. The final configuration file is then opened relative to the
    verified parent descriptor using O_NOFOLLOW and O_CLOEXEC.

    Returns the open file descriptor to workspaces.json, or None if the file
    (or any parent component) does not exist.
    """
    path = path.resolve() if not path.is_absolute() else path
    parent = path.parent
    filename = path.name
    parts = parent.parts
    if not parts or parts[0] != "/":
        raise WorkspaceConfigError("The workspace configuration path must be an absolute path.")

    try:
        cur_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise WorkspaceConfigError("The root directory could not be opened for configuration traversal.") from exc

    user_uid = os.getuid()
    for i, comp in enumerate(parts[1:]):
        is_immediate = (i == len(parts) - 2)
        try:
            child_fd = os.open(
                comp,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=cur_fd,
            )
        except OSError as exc:
            _close(cur_fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise WorkspaceConfigError(
                    f"Configuration directory component '{comp}' is not a directory or is a symlink."
                ) from exc
            if exc.errno == errno.ENOENT:
                return None
            raise WorkspaceConfigError(
                f"Configuration directory component '{comp}' could not be opened."
            ) from exc

        try:
            st = os.fstat(child_fd)
            if not stat.S_ISDIR(st.st_mode):
                raise WorkspaceConfigError(
                    f"Configuration directory component '{comp}' is not a directory."
                )
            if st.st_uid not in (0, user_uid):
                raise WorkspaceConfigError(
                    f"Configuration directory component '{comp}' is not owned by the current user or root."
                )
            if is_immediate:
                if st.st_uid != user_uid:
                    raise WorkspaceConfigError(
                        "The workspace configuration parent directory must be owned by the current user."
                    )
                if (st.st_mode & 0o002 != 0) and not (st.st_mode & 0o1000):
                    raise WorkspaceConfigError(
                        "The workspace configuration parent directory must not be world-writable."
                    )
        except Exception:
            _close(child_fd)
            _close(cur_fd)
            raise

        _close(cur_fd)
        cur_fd = child_fd

    try:
        try:
            file_fd = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=cur_fd,
            )
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise WorkspaceConfigError("The workspace configuration must not be a symlink.") from exc
            if exc.errno == errno.ENOENT:
                return None
            raise WorkspaceConfigError("The workspace configuration could not be opened.") from exc
        return file_fd
    finally:
        _close(cur_fd)


def load_workspace_config(config_path: Optional[Path] = None) -> WorkspaceConfig:
    """Load and validate the approved-root configuration.

    A missing file is an empty configuration. Any existing but malformed file,
    symlinked file, wrong-ownership file, insecure-permission file, or symlinked
    parent directory fails closed instead of being treated as "no workspaces".
    """
    path = Path(config_path).expanduser() if config_path is not None else default_config_path()

    fd = _open_config_file(path)
    if fd is None:
        return WorkspaceConfig.empty()

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise WorkspaceConfigError("The workspace configuration path is not a regular file.")

        current_uid = os.getuid()
        if st.st_uid != current_uid:
            raise WorkspaceConfigError(
                f"The workspace configuration file is not owned by the current user (UID {current_uid})."
            )

        mode = stat.S_IMODE(st.st_mode)
        # Permitted non-executable safe modes: 0600, 0644, 0400, 0444
        if mode not in (0o600, 0o644, 0o400, 0o444):
            raise WorkspaceConfigError(
                f"The workspace configuration has unsafe or unexpected permissions ({oct(mode)}). "
                f"Only safe non-executable modes (0600, 0644, 0400, 0444) are permitted."
            )

        if st.st_size > MAX_CONFIG_BYTES:
            raise WorkspaceConfigError("The workspace configuration is too large.")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CONFIG_BYTES:
                raise WorkspaceConfigError("The workspace configuration is too large.")
        raw = b"".join(chunks)
    finally:
        _close(fd)

    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceConfigError("The workspace configuration is not valid JSON.") from exc

    if not isinstance(data, dict):
        raise WorkspaceConfigError("The workspace configuration must be a JSON object.")
    unknown = set(data) - {"version", "workspaces"}
    if unknown:
        raise WorkspaceConfigError("The workspace configuration has unknown fields.")

    version = data.get("version", 1)
    if version != 1:
        raise WorkspaceConfigError("The workspace configuration version is unsupported.")

    workspaces = data.get("workspaces")
    if not isinstance(workspaces, dict):
        raise WorkspaceConfigError("'workspaces' must be a JSON object of id to path.")
    if len(workspaces) > MAX_WORKSPACES:
        raise WorkspaceConfigError("Too many workspaces are configured.")

    roots: dict[str, WorkspaceRoot] = {}
    folded_ids: set[str] = set()
    for workspace_id, raw_path in workspaces.items():
        if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.fullmatch(
            workspace_id
        ):
            raise WorkspaceConfigError("A workspace identifier is malformed.")
        folded = workspace_id.casefold()
        if folded in folded_ids:
            raise WorkspaceConfigError("Workspace identifiers must be unique.")
        folded_ids.add(folded)
        if not isinstance(raw_path, str):
            raise WorkspaceConfigError(
                f"Workspace '{workspace_id}' root must be a string path."
            )
        roots[workspace_id] = WorkspaceRoot.from_path(workspace_id, raw_path)

    return WorkspaceConfig(roots=MappingProxyType(roots))


def configured_workspace_ids(config_path: Optional[Path] = None) -> list[str]:
    """Return validated workspace IDs for prompt disclosure.

    A broken configuration returns no IDs rather than crashing conversation
    startup; the file tools themselves still report the configuration error.
    """
    try:
        return list(load_workspace_config(config_path).ids())
    except WorkspaceConfigError:
        return []


def validate_workspace_id(workspace_id: object) -> str:
    """Return a canonical workspace identifier or raise a config error."""
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.fullmatch(
        workspace_id
    ):
        raise WorkspaceConfigError("A workspace identifier is malformed.")
    return workspace_id


def _contains_sequence(parts: tuple[str, ...], sequences: tuple[tuple[str, ...], ...]) -> bool:
    for sequence in sequences:
        width = len(sequence)
        for index in range(0, len(parts) - width + 1):
            if parts[index : index + width] == sequence:
                return True
    return False


__all__ = [
    "MAX_CONFIG_BYTES",
    "MAX_WORKSPACES",
    "WORKSPACE_ID_MAX_LENGTH",
    "WORKSPACE_ID_PATTERN",
    "WorkspaceConfig",
    "WorkspaceConfigError",
    "WorkspaceRoot",
    "configured_workspace_ids",
    "default_config_path",
    "load_workspace_config",
    "validate_workspace_id",
]
