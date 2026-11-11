"""KIVO File Shelf subsystem.

Reference-based file staging, descriptor-relative path authorization, and
safe desktop application opening.
"""
from __future__ import annotations

from kivo.shelf.models import (
    MAX_BATCH_URIS,
    MAX_FILENAME_LENGTH,
    MAX_SHELF_ITEMS,
    MAX_URI_BYTES,
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
from kivo.shelf.service import FileShelfService, ShelfExportGrant

__all__ = [
    "MAX_BATCH_URIS",
    "MAX_FILENAME_LENGTH",
    "MAX_SHELF_ITEMS",
    "MAX_URI_BYTES",
    "AuthorizedFileDescriptor",
    "FileIdentity",
    "FileShelfService",
    "PathAuthorizer",
    "ShelfExportGrant",
    "ShelfItem",
    "ShelfPolicyError",
    "infer_file_type",
    "parse_and_validate_file_uri",
    "sanitize_filename",
]
