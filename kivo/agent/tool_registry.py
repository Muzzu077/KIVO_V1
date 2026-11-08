"""Registry for managing and discovering agent tools."""
from __future__ import annotations

import threading
from typing import Optional

from kivo.agent.tool_schema import ToolDefinition


class ToolRegistry:
    """Thread-safe registry for KIVO agent tools."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool_def: ToolDefinition) -> None:
        """Register a new tool definition."""
        if not isinstance(tool_def, ToolDefinition):
            raise TypeError("tool_def must be an instance of ToolDefinition")
        with self._lock:
            self._tools[tool_def.name] = tool_def

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        with self._lock:
            self._tools.pop(name, None)

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Lookup a registered tool by name."""
        with self._lock:
            return self._tools.get(name)

    def has_tool(self, name: str) -> bool:
        """Check if a tool is registered."""
        with self._lock:
            return name in self._tools

    def get_openai_tools(self) -> list[dict]:
        """Export all registered tools in OpenAI-compatible JSON schema format."""
        with self._lock:
            return [t.to_openai_tool() for t in self._tools.values()]

    def list_tool_names(self) -> list[str]:
        """Return names of all registered tools."""
        with self._lock:
            return sorted(self._tools.keys())

    def list_mutating_tool_names(self) -> list[str]:
        """Return names of tools marked as filesystem mutations.

        Stage 11.2 registry construction remains strictly read-only; this helper
        exists so lifecycle and security checks can prove that invariant without
        inspecting private registry state.
        """
        with self._lock:
            return sorted(
                name
                for name, tool in self._tools.items()
                if getattr(tool, "mutating", False)
            )

    def has_mutating_tools(self) -> bool:
        return bool(self.list_mutating_tool_names())

    def assert_read_only(self) -> None:
        """Fail if any registered tool claims mutation capability."""
        mutating = self.list_mutating_tool_names()
        if mutating:
            raise RuntimeError(
                "The default tool registry must remain read-only in Stage "
                "11.3.1a; mutating tool(s) registered: " + ", ".join(mutating)
            )
