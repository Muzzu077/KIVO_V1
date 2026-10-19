"""
actions/task_orchestrator_action.py — Multi-step task planner and executor tool.

Allows Gemini to decompose complex multi-step user instructions into an ordered sequence
of tool steps, execute them safely, verify intermediate states, and report progress.
"""
from __future__ import annotations

import json
from core.task_orchestrator import get_task_orchestrator


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

    plan = orchestrator.create_plan(goal, steps_raw)
    result_plan = orchestrator.execute_plan(plan, context=kwargs)

    report_lines = [f"Goal: {result_plan.goal}", f"Status: {result_plan.summary}", "Steps:"]
    for s in result_plan.steps:
        status_icon = "✅" if s.status.name == "COMPLETED" else "❌"
        report_lines.append(f"  {status_icon} Step {s.id}: {s.description} → {s.result or s.error}")

    return "\n".join(report_lines)


TOOL = {
    "name": "orchestrate_task",
    "description": (
        "Plans and executes multi-step workflows. "
        "Use this when a user request requires multiple steps across different tools "
        "(e.g., check disk usage, find large files, then organize them; or check weather and set reminder). "
        "Each step specifies the target tool, action, parameters, and description."
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
            }
        },
        "required": ["goal", "steps"]
    },
    "handler": task_orchestrator_handler,
}
