"""
actions/task_orchestrator_action.py — Multi-step task planner and executor tool.

Allows Gemini to decompose complex multi-step user instructions into an ordered sequence
of tool steps, execute them safely, verify intermediate states, and report progress.

Every plan runs inside an AgentWorkspace: a directory for anything the job
produces, plus a task memory scope for findings that matter while it runs. The
workspace is what keeps "the parser fails on empty input" out of the user's
personal memory.
"""
from __future__ import annotations

import json

from core.task_orchestrator import get_task_orchestrator
from core.agent_workspace import (
    close_workspace,
    create_workspace,
    load_workspace,
    record_finding,
    set_last_workspace,
)


def task_orchestrator_handler(parameters: dict, **kwargs) -> str:
    orchestrator = get_task_orchestrator()
    goal = parameters.get("goal", "Multi-step workflow")
    steps_raw = parameters.get("steps", [])

    if isinstance(steps_raw, str):
        try:
            steps_raw = json.loads(steps_raw)
        except Exception:
            return "Invalid steps format. Expected a JSON list of step objects."

    if not isinstance(steps_raw, list) or not steps_raw:
        return "Please provide at least one step in the steps list."

    # Reuse a workspace when the model is continuing one, otherwise make a new
    # one. Creation is cheap and never writes into the user's repository.
    workspace_id = str(parameters.get("workspace_id", "") or "").strip()
    workspace = load_workspace(workspace_id) if workspace_id else None
    if workspace is None:
        try:
            workspace = create_workspace(goal, kind="task")
        except Exception as e:
            print(f"[Agent] ⚠️ Workspace unavailable: {e}")
            workspace = None
    else:
        set_last_workspace(workspace)

    plan = orchestrator.create_plan(
        goal, steps_raw, workspace_id=workspace.id if workspace else "")
    result_plan = orchestrator.execute_plan(plan, context=kwargs)

    if workspace is not None:
        record_finding(workspace, "result", result_plan.summary)
        for step in result_plan.steps:
            record_finding(
                workspace,
                f"step_{step.id}",
                f"{step.description}: {step.result or step.error}",
            )
        if result_plan.is_finished:
            close_workspace(workspace)

    report_lines = [f"Goal: {result_plan.goal}", f"Status: {result_plan.summary}", "Steps:"]
    for s in result_plan.steps:
        status_icon = "✅" if s.status.name == "COMPLETED" else "❌"
        report_lines.append(f"  {status_icon} Step {s.id}: {s.description} → {s.result or s.error}")
    if workspace is not None:
        report_lines.append(f"Workspace: {workspace.root}")
        report_lines.append(f"Memory scope: {workspace.scope}")

    return "\n".join(report_lines)


TOOL = {
    "name": "orchestrate_task",
    "description": (
        "Plans and executes multi-step workflows. "
        "Use this when a user request requires multiple steps across different tools "
        "(e.g., check disk usage, find large files, then organize them; or check weather and set reminder). "
        "Each step specifies the target tool, action, parameters, and description. "
        "The job runs in its own workspace with task-scoped memory; do not ask the "
        "user to 'start an agent' first — if the request needs several steps, this "
        "is the tool. Pass workspace_id to continue an existing workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "High-level description of what the multi-step task accomplishes."
            },
            "steps": {
                "type": "ARRAY",
                "description": "Ordered list of step objects. Each object has 'tool', 'action', 'parameters', and 'description'.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "tool": {"type": "STRING", "description": "Target tool name (e.g., file_controller, weather_report, computer_settings)"},
                        "action": {"type": "STRING", "description": "Action verb for the tool"},
                        "description": {"type": "STRING", "description": "Human-readable description of what this step does"},
                        "parameters": {"type": "OBJECT", "description": "Parameters dictionary passed to the target tool"}
                    },
                    "required": ["tool", "description"]
                }
            },
            "workspace_id": {
                "type": "STRING",
                "description": "Optional existing agent workspace id to continue. Omit to create a new one."
            }
        },
        "required": ["goal", "steps"]
    },
    "handler": task_orchestrator_handler,
}
