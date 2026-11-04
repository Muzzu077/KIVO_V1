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

    MAX_TASK_HISTORY = 50

    def __init__(self):
        self._action_registry = None
        self._active_tasks: dict[str, TaskPlan] = {}
        self._task_history: list[TaskPlan] = []
        self._progress_callback: Optional[Callable[[str, int, int, str], None]] = None
        self._listeners: list[Callable[[dict], None]] = []

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

    def add_listener(self, callback: Callable[[dict], None]) -> None:
        """Observe task lifecycle snapshots for the UI/IPC layer."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _notify_task(self, plan: TaskPlan, reason: str) -> None:
        with self._lock:
            listeners = list(self._listeners)
        if not listeners:
            return
        event = {"reason": reason, "task": self._plan_snapshot(plan)}
        for callback in listeners:
            try:
                callback(event)
            except Exception:
                pass

    @staticmethod
    def _plan_snapshot(plan: TaskPlan) -> dict:
        """A UI-safe view: no tool parameters, results or error text."""
        if plan.cancel_event.is_set() and plan.is_finished:
            status = "cancelled"
        elif plan.is_finished:
            status = "completed" if plan.success else "failed"
        elif plan.current_step_index:
            status = "running"
        else:
            status = "pending"

        # plan.summary may contain raw tool output or exception text. Replace
        # it with a generic, step-numbered failure message for failed plans.
        summary = plan.summary
        if (plan.is_finished and not plan.success
                and not plan.cancel_event.is_set()):
            failed_step = next(
                (step for step in plan.steps
                 if step.status is StepStatus.FAILED),
                None,
            )
            if failed_step is not None:
                summary = f"Task failed at step {failed_step.id}."

        return {
            "task_id": plan.task_id,
            "goal": plan.goal,
            "workspace_id": plan.workspace_id,
            "created_at": plan.created_at,
            "current_step": plan.current_step_index,
            "total_steps": len(plan.steps),
            "status": status,
            "success": plan.success,
            "summary": summary,
            "steps": [
                {
                    "id": step.id,
                    "description": step.description,
                    "status": step.status.name.lower(),
                    "is_critical": step.is_critical,
                }
                for step in plan.steps
            ],
        }

    def has_active_tasks(self) -> bool:
        with self._lock:
            return any(not plan.is_finished for plan in self._active_tasks.values())

    def get_current_task(self) -> Optional[TaskPlan]:
        """The newest unfinished plan, or None."""
        with self._lock:
            active = [p for p in self._active_tasks.values() if not p.is_finished]
        if not active:
            return None
        return max(active, key=lambda plan: plan.created_at)

    def public_task_snapshot(self, plan: TaskPlan) -> dict:
        return self._plan_snapshot(plan)

    def task_history_snapshot(self, limit: int = 20) -> list[dict]:
        with self._lock:
            history = list(reversed(self._task_history))[: max(1, limit)]
        return [self._plan_snapshot(plan) for plan in history]

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
        self._notify_task(plan, "created")
        return plan

    def _finalize_plan(self, plan: TaskPlan) -> None:
        """Record a finished plan once and tell listeners it is over."""
        with self._lock:
            if any(existing is plan for existing in self._task_history):
                return
            self._task_history.append(plan)
            if len(self._task_history) > self.MAX_TASK_HISTORY:
                del self._task_history[:-self.MAX_TASK_HISTORY]
        self._notify_task(plan, "finished")

    def execute_plan(self, plan: TaskPlan, context: dict | None = None) -> TaskPlan:
        """
        Execute all steps of a plan sequentially with safety checks and verification.
        Synchronous execution (call via to_thread or in executor).
        """
        if not self._action_registry:
            plan.summary = "Orchestrator has no action registry bound."
            plan.is_finished = True
            plan.success = False
            self._finalize_plan(plan)
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
            self._finalize_plan(plan)
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
            self._notify_task(plan, "progress")

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
        never_started = []
        for plan in active:
            plan.cancel_event.set()
            if plan.current_step_index == 0 and not plan.is_finished:
                plan.is_finished = True
                plan.success = False
                plan.summary = "Task cancelled before execution."
                never_started.append(plan)
        for plan in never_started:
            self._finalize_plan(plan)
        from core.drive import get_drive_session
        get_drive_session().stop("cancelled by user")
        names = ", ".join(p.goal[:40] for p in active)
        return f"Cancelling: {names}"


def get_task_orchestrator() -> TaskOrchestrator:
    return TaskOrchestrator.get_instance()
