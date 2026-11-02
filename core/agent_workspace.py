"""
core/agent_workspace.py — a home for work that takes more than one step.

WHY THIS EXISTS
    A multi-step job needs somewhere to put things: files it generates, notes
    about what it has tried, the goal it is working toward. Writing those into
    the current directory is how an assistant scatters `notes.txt` through a
    repository, and writing them into personal memory is how "the parser fails
    on empty input" ends up being recited as a fact about the user a month
    later.

    An AgentWorkspace is therefore three things at once:

      * a directory on disk, under the user's data directory, not the repo;
      * a memory scope — task notes for this job, agent notes that survive it;
      * a small JSON record so the job can be listed, resumed or closed.

    It is deliberately not a sandbox and not a second orchestrator. The existing
    TaskOrchestrator still plans and runs the steps; this only gives the plan an
    identity and a place to remember things.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from memory.memory_manager import (
    format_layered_memory_for_prompt,
    remember_layer,
    get_layered_memory,
)


def _data_dir() -> Path:
    override = os.environ.get("MARK_LV_AGENTS_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "mark-lv" / "agents"


def _slug(text: str, limit: int = 40) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (text[:limit] or "task").strip("-")


@dataclass
class AgentWorkspace:
    id: str
    goal: str
    root: Path
    kind: str = "agent"
    created_at: float = field(default_factory=time.time)
    status: str = "active"

    @property
    def scope(self) -> str:
        """The memory scope for this workspace (task and agent layers)."""
        return self.id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "root": str(self.root),
            "kind": self.kind,
            "created_at": self.created_at,
            "status": self.status,
        }


_last_workspace: Optional[AgentWorkspace] = None


def get_last_workspace() -> Optional[AgentWorkspace]:
    """The most recently created workspace, for the caller that just made it."""
    return _last_workspace


def set_last_workspace(workspace: Optional[AgentWorkspace]) -> None:
    """Let a resumed workspace become the active one again."""
    global _last_workspace
    _last_workspace = workspace


def create_workspace(
    goal: str,
    kind: str = "agent",
    base_dir: Optional[Path] = None,
) -> AgentWorkspace:
    """Create a directory and a memory scope for a multi-step job."""
    global _last_workspace
    goal = (goal or "Untitled task").strip()[:500]
    root_base = Path(base_dir).expanduser() if base_dir else _data_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    workspace_id = f"{_slug(goal)}-{stamp}-{secrets.token_hex(2)}"
    root = root_base / workspace_id
    root.mkdir(parents=True, exist_ok=True)

    ws = AgentWorkspace(id=workspace_id, goal=goal, root=root, kind=kind)
    (root / "workspace.json").write_text(
        json.dumps(ws.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    remember_layer("agent", "goal", goal, ws.scope, source="workspace")
    remember_layer("agent", "status", "active", ws.scope, source="workspace")
    remember_layer("task", "goal", goal, ws.scope, source="workspace")
    _last_workspace = ws
    print(f"[Agent] 📁 Workspace created: {root}")
    return ws


def load_workspace(workspace_id: str, base_dir: Optional[Path] = None) -> Optional[AgentWorkspace]:
    if not workspace_id:
        return None
    root = (Path(base_dir).expanduser() if base_dir else _data_dir()) / workspace_id
    record = root / "workspace.json"
    if not record.exists():
        return None
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        return AgentWorkspace(
            id=data.get("id", workspace_id),
            goal=data.get("goal", ""),
            root=Path(data.get("root", root)),
            kind=data.get("kind", "agent"),
            created_at=float(data.get("created_at", 0.0)),
            status=data.get("status", "active"),
        )
    except Exception as e:
        print(f"[Agent] ⚠️ Could not read workspace {workspace_id}: {e}")
        return None


def list_workspaces(base_dir: Optional[Path] = None, limit: int = 20) -> list[AgentWorkspace]:
    root = Path(base_dir).expanduser() if base_dir else _data_dir()
    if not root.exists():
        return []
    found = []
    for child in root.iterdir():
        if child.is_dir() and (child / "workspace.json").exists():
            ws = load_workspace(child.name, root)
            if ws:
                found.append(ws)
    found.sort(key=lambda w: w.created_at, reverse=True)
    return found[:limit]


def close_workspace(workspace: AgentWorkspace | str) -> str:
    ws = workspace if isinstance(workspace, AgentWorkspace) else load_workspace(str(workspace))
    if ws is None:
        return "Workspace not found."
    ws.status = "closed"
    try:
        (ws.root / "workspace.json").write_text(
            json.dumps(ws.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        return f"Could not update workspace: {e}"
    remember_layer("agent", "status", "closed", ws.scope, source="workspace")
    return f"Workspace closed: {ws.id}"


def record_finding(
    workspace: AgentWorkspace | str,
    key: str,
    value: str,
    *,
    durable: bool = False,
    sensitive: bool = False,
) -> str:
    """Record something learned while working.

    `durable=False` puts it in task memory, which is the right default: a
    compiler error matters to this job, not to every future conversation.
    """
    ws = workspace if isinstance(workspace, AgentWorkspace) else load_workspace(str(workspace))
    if ws is None:
        return "Workspace not found."
    layer = "agent" if durable else "task"
    return remember_layer(layer, key, value, ws.scope, sensitive=sensitive, source="agent")


def workspace_memory_block(workspace: AgentWorkspace | str) -> str:
    ws = workspace if isinstance(workspace, AgentWorkspace) else load_workspace(str(workspace))
    if ws is None:
        return ""
    return format_layered_memory_for_prompt(agent=ws.scope, task=ws.scope).strip()


def recent_workspaces_text(limit: int = 5) -> str:
    workspaces = list_workspaces(limit=limit)
    if not workspaces:
        return "No agent workspaces yet."
    lines = ["Agent workspaces:"]
    for ws in workspaces:
        lines.append(f"  - {ws.id} [{ws.status}] {ws.goal[:80]}")
    return "\n".join(lines)
