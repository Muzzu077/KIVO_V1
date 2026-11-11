"""File Shelf service for managing transient, reference-based file staging."""
from __future__ import annotations

import os
import shutil
import threading
import uuid
from typing import Callable, Sequence

from kivo.core.bounded_process import BoundedProcessResult, run_bounded
from kivo.shelf.models import (
    MAX_BATCH_URIS,
    MAX_SHELF_ITEMS,
    FileIdentity,
    ShelfItem,
    infer_file_type,
    sanitize_filename,
)
from kivo.shelf.path_policy import (
    AuthorizedFileDescriptor,
    PathAuthorizer,
    ShelfPolicyError,
    parse_and_validate_file_uri,
)


class ShelfExportGrant:
    """One explicit desktop authorization to export a staged file.

    Staging a file on the File Shelf does **not** authorize export.  This
    object is only produced by :meth:`FileShelfService.authorize_export`, which
    revalidates the path and retains a verified read descriptor.  The grant
    owns that descriptor until :meth:`close` is called.
    """

    def __init__(self, item: ShelfItem, descriptor: AuthorizedFileDescriptor):
        self.item_id = item.id
        self.name = item.name
        self.file_type = item.file_type
        self.size_bytes = item.size_bytes
        self.canonical_path = descriptor.canonical_path
        self.identity = descriptor.identity
        self._descriptor = descriptor

    @property
    def fd(self) -> int:
        return self._descriptor.fd

    @property
    def closed(self) -> bool:
        return self._descriptor.closed

    def close(self) -> None:
        self._descriptor.close()

    def __enter__(self) -> "ShelfExportGrant":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class FileShelfService:
    """Service providing temporary in-memory file reference staging."""

    def __init__(
        self,
        on_items_changed: Callable[[list[ShelfItem]], None] | None = None,
        *,
        authorizer: PathAuthorizer | None = None,
        xdg_open_path: str | None = None,
        runner: Callable[..., BoundedProcessResult] = run_bounded,
    ):
        self._on_items_changed = on_items_changed
        self._authorizer = authorizer if authorizer is not None else PathAuthorizer()
        self._runner = runner
        self._xdg_open = xdg_open_path or shutil.which("xdg-open")

        self._lock = threading.Lock()
        self._items: dict[str, ShelfItem] = {}
        self._closed = False

    # ── Service Lifecycle ──────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the file shelf service."""
        if self._closed:
            return False
        return True

    def close(self) -> None:
        """Clear all in-memory references on shutdown."""
        self._closed = True
        with self._lock:
            self._items.clear()

    # ── Operations ─────────────────────────────────────────────────────────

    def add_items(
        self, uris: Sequence[str]
    ) -> tuple[list[ShelfItem], list[dict[str, str]]]:
        """Validate, authorize, and stage local file URIs as references.

        Returns:
            (added_items, rejected_entries)
        """
        if self._closed:
            raise RuntimeError("FileShelfService is closed")

        if not isinstance(uris, (list, tuple)):
            raise ShelfPolicyError("invalid_params", "uris must be a list of URI strings")

        if len(uris) > MAX_BATCH_URIS:
            raise ShelfPolicyError(
                "batch_too_large",
                f"Batch exceeds maximum of {MAX_BATCH_URIS} URIs per request",
            )

        added: list[ShelfItem] = []
        rejected: list[dict[str, str]] = []

        with self._lock:
            # Build fast lookup of already-staged canonical paths
            staged_paths = {item._canonical_path: item for item in self._items.values()}

            for uri in uris:
                try:
                    path = parse_and_validate_file_uri(uri)
                    canonical, identity, is_ws, ws_name = (
                        self._authorizer.authorize_and_inspect(path)
                    )

                    # Deduplication check
                    if canonical in staged_paths:
                        # Already staged; include existing item without duplication
                        added.append(staged_paths[canonical])
                        continue

                    # Shelf capacity limit
                    if len(self._items) >= MAX_SHELF_ITEMS:
                        rejected.append({
                            "uri": uri,
                            "code": "shelf_full",
                            "message": f"Shelf has reached maximum capacity of {MAX_SHELF_ITEMS} items",
                        })
                        continue

                    # Generate opaque random ID
                    item_id = f"shelf_{uuid.uuid4().hex[:12]}"
                    basename = os.path.basename(canonical)
                    name = sanitize_filename(basename)
                    file_type = infer_file_type(name)

                    item = ShelfItem(
                        id=item_id,
                        name=name,
                        file_type=file_type,
                        size_bytes=identity.size,
                        _canonical_path=canonical,
                        _identity=identity,
                        _is_workspace=is_ws,
                        _workspace_name=ws_name,
                    )

                    self._items[item_id] = item
                    staged_paths[canonical] = item
                    added.append(item)

                except ShelfPolicyError as exc:
                    rejected.append({
                        "uri": uri,
                        "code": exc.code,
                        "message": exc.message,
                    })
                except Exception as exc:
                    rejected.append({
                        "uri": uri,
                        "code": "staging_failed",
                        "message": str(exc),
                    })

            if added:
                current_list = list(self._items.values())

        if added:
            self._notify_changed(current_list)

        return added, rejected

    def list_items(self) -> list[ShelfItem]:
        """List current staged items with real-time freshness checks."""
        with self._lock:
            items_copy = list(self._items.values())

        updated: list[ShelfItem] = []
        changed = False

        for item in items_copy:
            # Lightweight verification of accessibility
            valid = self._authorizer.verify_existing_identity(
                item._canonical_path, item._identity
            )
            if not valid and (not item.stale or item.availability != "unavailable"):
                stale_item = ShelfItem(
                    id=item.id,
                    name=item.name,
                    file_type=item.file_type,
                    size_bytes=item.size_bytes,
                    staged_at=item.staged_at,
                    stale=True,
                    availability="unavailable",
                    _canonical_path=item._canonical_path,
                    _identity=item._identity,
                    _is_workspace=item._is_workspace,
                    _workspace_name=item._workspace_name,
                )
                with self._lock:
                    if item.id in self._items:
                        self._items[item.id] = stale_item
                updated.append(stale_item)
                changed = True
            else:
                updated.append(item)

        if changed:
            self._notify_changed(updated)

        return updated

    def remove_item(self, item_id: str) -> bool:
        """Remove a staged file reference (underlying disk file is untouched)."""
        with self._lock:
            if item_id in self._items:
                del self._items[item_id]
                current_list = list(self._items.values())
                removed = True
            else:
                removed = False

        if removed:
            self._notify_changed(current_list)
        return removed

    def clear(self) -> bool:
        """Clear all staged references without deleting any files."""
        with self._lock:
            self._items.clear()

        self._notify_changed([])
        return True

    def open_item(self, item_id: str) -> tuple[bool, str | None, str | None]:
        """Safely open an item with the desktop's default application (xdg-open).

        Revalidates identity, rejects stale or replaced files, and enforces timeout.
        """
        if self._closed:
            return False, "service_unavailable", "FileShelfService is closed"

        with self._lock:
            item = self._items.get(item_id)

        if item is None:
            return False, "item_not_found", f"Item '{item_id}' not found on File Shelf"

        # 1. Revalidate filesystem identity before opening (TOCTOU / replacement check)
        valid = self._authorizer.verify_existing_identity(
            item._canonical_path, item._identity
        )
        if not valid:
            # Mark reference stale
            with self._lock:
                if item_id in self._items:
                    self._items[item_id] = ShelfItem(
                        id=item.id,
                        name=item.name,
                        file_type=item.file_type,
                        size_bytes=item.size_bytes,
                        staged_at=item.staged_at,
                        stale=True,
                        availability="unavailable",
                        _canonical_path=item._canonical_path,
                        _identity=item._identity,
                        _is_workspace=item._is_workspace,
                        _workspace_name=item._workspace_name,
                    )
            return (
                False,
                "stale_reference",
                "File was modified, replaced, or deleted on disk since staging",
            )

        # 2. Check xdg-open availability
        if not self._xdg_open:
            return False, "xdg_open_missing", "xdg-open executable not found"

        # 3. Launch via bounded subprocess (fixed binary, argv array, shell=False)
        result = self._runner(
            [self._xdg_open, item._canonical_path],
            timeout=2.0,
        )

        if result.timed_out:
            return False, "timeout", "Opening file with xdg-open timed out"
        if result.returncode != 0:
            err = result.stderr.strip() or f"exit code {result.returncode}"
            return False, "launch_failed", f"Failed to open file: {err}"

        return True, None, None

    def authorize_export(self, item_id: str) -> ShelfExportGrant:
        """Explicitly authorize exporting one staged item to a remote device.

        This is a distinct user action: staging alone grants nothing.  The
        path is revalidated through the descriptor-relative policy, the file
        must still match the staged identity exactly, and a verified read
        descriptor is retained for the caller.

        Raises:
            ShelfPolicyError: unknown item, stale/replaced file, symlink, or
                policy violation (including sensitive paths).
        """
        if self._closed:
            raise ShelfPolicyError("service_unavailable", "FileShelfService is closed")

        with self._lock:
            item = self._items.get(item_id)

        if item is None:
            raise ShelfPolicyError("item_not_found", f"Item '{item_id}' not found on File Shelf")

        try:
            descriptor = self._authorizer.open_authorized_descriptor(item._canonical_path)
        except ShelfPolicyError as exc:
            self._mark_stale(item)
            raise ShelfPolicyError(
                "stale_reference",
                "File was modified, replaced, or deleted on disk since staging",
            ) from exc

        try:
            if not self._identity_matches(item._identity, descriptor.identity):
                raise ShelfPolicyError(
                    "stale_reference",
                    "File was modified, replaced, or deleted on disk since staging",
                )
            return ShelfExportGrant(item, descriptor)
        except BaseException:
            descriptor.close()
            self._mark_stale(item)
            raise

    def _mark_stale(self, item: ShelfItem) -> None:
        with self._lock:
            current = self._items.get(item.id)
            if current is None or (current.stale and current.availability == "unavailable"):
                return
            self._items[item.id] = ShelfItem(
                id=current.id,
                name=current.name,
                file_type=current.file_type,
                size_bytes=current.size_bytes,
                staged_at=current.staged_at,
                stale=True,
                availability="unavailable",
                _canonical_path=current._canonical_path,
                _identity=current._identity,
                _is_workspace=current._is_workspace,
                _workspace_name=current._workspace_name,
            )

    @staticmethod
    def _identity_matches(expected: FileIdentity, actual: FileIdentity) -> bool:
        if (expected.dev, expected.ino) != (actual.dev, actual.ino):
            return False
        if expected.size != actual.size or expected.mtime_ns != actual.mtime_ns:
            return False
        if expected.mode != actual.mode:
            return False
        if (
            expected.mnt_id is not None
            and actual.mnt_id is not None
            and expected.mnt_id != actual.mnt_id
        ):
            return False
        return True

    def _notify_changed(self, items: list[ShelfItem]) -> None:
        callback = self._on_items_changed
        if callback is not None and not self._closed:
            try:
                callback(items)
            except Exception:
                pass


__all__ = ["FileShelfService"]
