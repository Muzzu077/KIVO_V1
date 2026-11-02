"""
core/task_orchestrator.py — Multi-step task planning, execution, and verification engine.

WHY THIS EXISTS
    Single tool calls can only handle atomic actions. Real-world user requests often
    require executing a sequence of steps:
      1. Plan the steps required to achieve a goal.
      2. Check permissions & safety policy for each step.
      3. Execute each step sequentially, verifying the outcome before proceeding.
      4. If a step fails, attempt recovery or rollback previously executed reversible steps.
      5. Provide clear real-time progress feedback to the user and HUD.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional

from core.action_policy import check as policy_check, Control, Risk
from core.undo import push_undo, undo_last


class StepStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    SKIPPED = auto()
    ROLLED_BACK = auto()


@dataclass
class TaskStep:
    id: int
    tool: str
    action: str
    parameters: dict = field(default_factory=dict)
    description: str = ""
    status: StepStatus = StepStatus.PENDING
    result: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    is_critical: bool = True     # If True, failure stops the whole task


@dataclass
class TaskPlan:
    task_id: str
    goal: str
    steps: list[TaskStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    current_step_index: int = 0
    is_finished: bool = False
    success: bool = False
    summary: str = ""
    workspace_id: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)


class TaskOrchestrator:
    """Singleton multi-step task orchestrator."""

    _instance: Optional[TaskOrchestrator] = None
    _lock = threading.Lock()

    def __init__(self):
        self._action_registry = None
        self._active_tasks: dict[str, TaskPlan] = {}
        self._task_history: list[TaskPlan] = []
        self._progress_callback: Optional[Callable[[str, int, int, str], None]] = None

    @classmethod
    def get_instance(cls) -> TaskOrchestrator:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def bind_registry(self, registry) -> None:
        """Bind the active ActionRegistry for executing tool steps."""
        self._action_registry = registry

    def set_progress_callback(self, cb: Callable[[str, int, int, str], None]) -> None:
        """cb(task_id, current_step, total_steps, message)"""
        self._progress_callback = cb

    def create_plan(self, goal: str, steps_data: list[dict],
                    workspace_id: str = "") -> TaskPlan:
        """Create a plan from a list of step specifications."""
        task_id = f"task_{int(time.time() * 1000)}"
        steps = []
        for i, s in enumerate(steps_data):
            tool = s.get("tool", "")
            action = s.get("action", s.get("parameters", {}).get("action", "*"))
            params = s.get("parameters", {})
            if "action" not in params and action != "*":
                params["action"] = action
            desc = s.get("description", f"Run {tool}:{action}")
            is_crit = s.get("is_critical", True)

            steps.append(TaskStep(
                id=i + 1,
                tool=tool,
                action=action,
                parameters=params,
                description=desc,
                is_critical=is_crit,
            ))

        plan = TaskPlan(task_id=task_id, goal=goal, steps=steps,
                        workspace_id=workspace_id)
        with self._lock:
            self._active_tasks[task_id] = plan
        return plan

    def execute_plan(self, plan: TaskPlan, context: dict | None = None) -> TaskPlan:
        """
        Execute all steps of a plan sequentially with safety checks and verification.
        Synchronous execution (call via to_thread or in executor).
        """
        if not self._action_registry:
            plan.summary = "Orchestrator has no action registry bound."
            plan.is_finished = True
            plan.success = False
            return plan

        # Tell the Drive session that sustained work is happening. The UI can
        # show this, and both the voice path and the on-screen button can call
        # drive.stop() to set the cancel_event.
        from core.drive import get_drive_session
        drive = get_drive_session()
        drive.start(plan.goal)

        total = len(plan.steps)
        completed_count = 0
        executed_steps_for_rollback = []

        try:
            return self._run_steps(plan, context, total, completed_count,
                                   executed_steps_for_rollback, drive)
        finally:
            drive.stop("finished" if plan.success else (plan.summary or "ended"))

    def _run_steps(self, plan, context, total, completed_count,
                   executed_steps_for_rollback, drive):
        """Inner loop split out so the Drive session always stops in finally."""

        for idx, step in enumerate(plan.steps):
            if plan.cancel_event.is_set():
                for rem_idx in range(idx, total):
                    plan.steps[rem_idx].status = StepStatus.SKIPPED
                plan.is_finished = True
                plan.success = False
                plan.summary = f"Task cancelled before step {idx + 1}."
                return plan

            plan.current_step_index = idx + 1
            step.status = StepStatus.RUNNING
            step.started_at = time.monotonic()
            drive.update(step.description, plan.goal)

            if self._progress_callback:
                try:
                    self._progress_callback(plan.task_id, idx + 1, total, step.description)
                except Exception:
                    pass

            # Safety Policy Gate Check
            risk, control, denial = policy_check(step.tool, step.action, step.parameters)
            if denial:
                step.status = StepStatus.FAILED
                step.error = f"Policy denied: {denial}"
                step.finished_at = time.monotonic()
                if step.is_critical:
                    for rem_idx in range(idx + 1, total):
                        plan.steps[rem_idx].status = StepStatus.SKIPPED
                    plan.is_finished = True
                    plan.success = False
                    plan.summary = f"Task aborted at step {idx+1} ({step.description}): {denial}"
                    return plan
                continue

            # Execute Step via Action Registry
            try:
                res = self._action_registry.run(step.tool, step.parameters, context or {})
                step.result = str(res)
                step.finished_at = time.monotonic()

                # Basic failure heuristic in output
                is_err = "failed:" in res.lower() or "error:" in res.lower() or "crashed" in res.lower()
                if is_err:
                    step.status = StepStatus.FAILED
                    step.error = res
                    if step.is_critical:
                        for rem_idx in range(idx + 1, total):
                            plan.steps[rem_idx].status = StepStatus.SKIPPED
                        plan.is_finished = True
                        plan.success = False
                        plan.summary = f"Task failed at step {idx+1} ({step.description}): {res}"
                        return plan
                else:
                    step.status = StepStatus.COMPLETED
                    completed_count += 1
                    executed_steps_for_rollback.append(step)

            except Exception as e:
                step.status = StepStatus.FAILED
                step.error = str(e)
                step.finished_at = time.monotonic()
                if step.is_critical:
                    for rem_idx in range(idx + 1, total):
                        plan.steps[rem_idx].status = StepStatus.SKIPPED
                    plan.is_finished = True
                    plan.success = False
                    plan.summary = f"Task failed at step {idx+1} ({step.description}): {e}"
                    return plan

        plan.is_finished = True
        plan.success = completed_count == total
        plan.summary = f"Task completed successfully: {completed_count}/{total} steps executed."
        return plan

    def get_task(self, task_id: str) -> Optional[TaskPlan]:
        with self._lock:
            return self._active_tasks.get(task_id)

    def cancel_active(self) -> str:
        """Cancel every running plan. Called when the user says 'stop'."""
        with self._lock:
            active = [p for p in self._active_tasks.values()
                      if not p.is_finished]
        if not active:
            return "No active tasks to cancel."
        for plan in active:
            plan.cancel_event.set()
        from core.drive import get_drive_session
        get_drive_session().stop("cancelled by user")
        names = ", ".join(p.goal[:40] for p in active)
        return f"Cancelling: {names}"


def get_task_orchestrator() -> TaskOrchestrator:
    return TaskOrchestrator.get_instance()
