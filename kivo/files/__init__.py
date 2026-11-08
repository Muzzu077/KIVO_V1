"""Read-only file intelligence over explicitly approved workspace roots."""
from __future__ import annotations

from kivo.files.service import FileIntelligenceError, FileIntelligenceService
from kivo.files.tools import (
    BoundedFileWorker,
    FILE_INTELLIGENCE_TOOL_NAMES,
    create_file_tools,
)
from kivo.files.workspace import (
    WorkspaceConfig,
    WorkspaceConfigError,
    WorkspaceRoot,
    configured_workspace_ids,
    default_config_path,
    load_workspace_config,
)

__all__ = [
    "BoundedFileWorker",
    "FILE_INTELLIGENCE_TOOL_NAMES",
    "FileIntelligenceError",
    "FileIntelligenceService",
    "WorkspaceConfig",
    "WorkspaceConfigError",
    "WorkspaceRoot",
    "configured_workspace_ids",
    "create_file_tools",
    "default_config_path",
    "load_workspace_config",
]
