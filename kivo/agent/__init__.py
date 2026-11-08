"""KIVO Agent runtime and tool subsystem."""
from __future__ import annotations

from typing import Any, Optional

from kivo.agent.action_adapter import ActionAdapter, STAGE_11_LEGACY_SPECS
from kivo.agent.agent_runtime import AgentRuntime
from kivo.agent.approval_context import (
    APPROVAL_CONTEXT_VERSION,
    ApprovalContext,
    ApprovalContextError,
    ApprovalDisplay,
    MutationBinding,
    MutationPreflight,
    validate_mutation_grant,
)
from kivo.agent.approval_gate import (
    ApprovalGate,
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
)
from kivo.agent.authorization import (
    AuthorizationAction,
    AuthorizationDecision,
    AuthorizationPolicy,
)
from kivo.agent.mutation_budget import MutationBudget
from kivo.agent.terminal import create_terminal_tool
from kivo.agent.tool_executor import ToolExecutor
from kivo.agent.tool_policy import ToolPolicyError
from kivo.agent.tool_registry import ToolRegistry
from kivo.agent.tool_schema import (
    ExecutionGrant,
    LegacyActionBinding,
    PermissionLevel,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from kivo.agent.tools.system_info import SYSTEM_OVERVIEW_TOOL
from kivo.agent.tools.time_info import CURRENT_TIME_TOOL


def create_default_tool_registry(
    approval_gate: Optional[ApprovalGate] = None,
    action_registry: Optional[Any] = None,
    file_service: Optional[Any] = None,
    file_worker: Optional[Any] = None,
) -> ToolRegistry:
    """Create a ToolRegistry with safe built-ins and an explicit legacy allowlist.

    Legacy actions are exposed only through ``ActionAdapter`` and only the
    Stage 11 read-only specs. Discovery alone never makes an action callable by
    the conversational agent.

    The five Stage 11.2 filesystem tools are always registered, but they can
    only operate on roots explicitly listed in the user's workspace
    configuration. Without that configuration they return a helpful
    ``workspace_not_configured`` error instead of falling back to the home
    directory or the process working directory.

    No mutating filesystem tool is registered here in Stage 11.3.1a.  The
    optional ``file_worker`` lets the application own the read-only worker's
    lifecycle.
    """
    from kivo.files.service import FileIntelligenceService
    from kivo.files.tools import create_file_tools

    registry = ToolRegistry()
    registry.register(CURRENT_TIME_TOOL)
    registry.register(SYSTEM_OVERVIEW_TOOL)
    for tool in create_file_tools(
        file_service or FileIntelligenceService(),
        worker=file_worker,
    ):
        registry.register(tool)
    if approval_gate is not None:
        registry.register(create_terminal_tool(approval_gate))
    if action_registry is not None:
        adapter = ActionAdapter(action_registry, specs=list(STAGE_11_LEGACY_SPECS))
        for tool in adapter.build_tools():
            registry.register(tool)
    # Foundation invariant: normal registry construction never exposes a
    # filesystem mutation. Stage 11.3.1b must explicitly relax this check.
    registry.assert_read_only()
    return registry


__all__ = [
    "AgentRuntime",
    "ActionAdapter",
    "APPROVAL_CONTEXT_VERSION",
    "ApprovalContext",
    "ApprovalContextError",
    "ApprovalDisplay",
    "ApprovalGate",
    "ApprovalKind",
    "ApprovalRequest",
    "ApprovalStatus",
    "AuthorizationAction",
    "AuthorizationDecision",
    "AuthorizationPolicy",
    "ExecutionGrant",
    "LegacyActionBinding",
    "MutationBinding",
    "MutationBudget",
    "MutationPreflight",
    "STAGE_11_LEGACY_SPECS",
    "ToolRegistry",
    "ToolExecutor",
    "ToolDefinition",
    "ToolCall",
    "ToolResult",
    "PermissionLevel",
    "ToolPolicyError",
    "CURRENT_TIME_TOOL",
    "SYSTEM_OVERVIEW_TOOL",
    "FILE_INTELLIGENCE_TOOL_NAMES",
    "FileIntelligenceService",
    "create_default_tool_registry",
    "create_file_tools",
    "validate_mutation_grant",
]


def __getattr__(name: str) -> Any:
    if name in ("FILE_INTELLIGENCE_TOOL_NAMES", "create_file_tools"):
        from kivo.files.tools import FILE_INTELLIGENCE_TOOL_NAMES, create_file_tools

        return locals()[name]
    if name == "FileIntelligenceService":
        from kivo.files.service import FileIntelligenceService

        return FileIntelligenceService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
