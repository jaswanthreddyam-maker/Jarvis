from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from assistant.contracts import ActionResult, StepState, TaskPlan, TaskState


class TaskStateManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskState] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def create_task(self, user_input: str, plan: TaskPlan) -> TaskState:
        task_id = uuid4().hex
        steps = [
            StepState(
                step_id=f"{task_id}-step-{index + 1}",
                plan_step_id=step.step_id or (index + 1),
                action=step.action,
                target=step.target,
                params=dict(step.params),
                description=step.description,
                depends_on=tuple(step.depends_on),
                param_bindings=dict(step.param_bindings),
                verification=step.verification,
                fallback_action=step.fallback_action,
                fallback_target=step.fallback_target,
                fallback_params=dict(step.fallback_params),
                fallback_description=step.fallback_description,
                fallback_verification=step.fallback_verification,
                max_retries=step.max_retries,
                retry_group=step.retry_group,
                risk_level=step.risk_level.value if hasattr(step.risk_level, "value") else str(step.risk_level),
                expected_window=step.expected_window,
            )
            for index, step in enumerate(plan.steps)
        ]
        task = TaskState(
            task_id=task_id,
            user_input=user_input,
            intent=plan.goal or plan.intent,
            status="pending",
            steps=steps,
        )
        self._tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> TaskState:
        return self._tasks[task_id]

    def mark_task_running(self, task_id: str) -> None:
        task = self.get_task(task_id)
        task.status = "running"
        task.updated_at = self._now()

    def mark_task_completed(self, task_id: str) -> None:
        task = self.get_task(task_id)
        task.status = "completed"
        task.updated_at = self._now()

    def mark_task_failed(self, task_id: str, error: str | None = None) -> None:
        task = self.get_task(task_id)
        task.status = "failed"
        if error:
            task.final_response = error
        task.updated_at = self._now()

    def mark_task_interrupted(self, task_id: str) -> None:
        """Mark a task as interrupted (clean stop mid-sequence)."""
        task = self.get_task(task_id)
        task.status = "interrupted"
        task.updated_at = self._now()

    def mark_step_running(self, task_id: str, step_id: str) -> None:
        step = self._find_step(task_id, step_id)
        step.status = "running"
        step.attempts += 1
        self.get_task(task_id).updated_at = self._now()

    def mark_step_completed(self, task_id: str, step_id: str, result: ActionResult) -> None:
        step = self._find_step(task_id, step_id)
        step.status = "completed"
        step.result = dict(result.data)
        step.message = result.message
        step.error = result.error
        self.get_task(task_id).updated_at = self._now()

    def mark_step_failed(self, task_id: str, step_id: str, result: ActionResult) -> None:
        step = self._find_step(task_id, step_id)
        step.status = "failed"
        step.result = dict(result.data)
        step.message = result.message
        step.error = result.error or result.message
        self.get_task(task_id).updated_at = self._now()

    def mark_step_skipped(self, task_id: str, step_id: str, reason: str = "") -> None:
        """Mark a step as skipped (e.g. due to interrupt or validation failure)."""
        step = self._find_step(task_id, step_id)
        step.status = "skipped"
        step.message = reason
        self.get_task(task_id).updated_at = self._now()

    def set_final_response(self, task_id: str, response: str) -> None:
        task = self.get_task(task_id)
        task.final_response = response
        task.updated_at = self._now()

    def snapshot(self, task_id: str) -> dict[str, object]:
        task = self.get_task(task_id)
        snapshot = asdict(task)
        snapshot["created_at"] = task.created_at.isoformat()
        snapshot["updated_at"] = task.updated_at.isoformat()
        return snapshot

    def _find_step(self, task_id: str, step_id: str) -> StepState:
        task = self.get_task(task_id)
        for step in task.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(f"Unknown step_id: {step_id}")


class StateManager(TaskStateManager):
    pass
