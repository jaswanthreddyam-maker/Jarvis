from __future__ import annotations

import time
from dataclasses import dataclass

from assistant.contracts import RiskLevel, TaskPlan


_AFFIRMATIVE_REPLIES = {
    "yes",
    "y",
    "ok",
    "okay",
    "sure",
    "approve",
    "approved",
    "go ahead",
    "do it",
    "please do",
    "confirm",
}
_NEGATIVE_REPLIES = {
    "no",
    "n",
    "cancel",
    "stop",
    "abort",
    "decline",
    "never mind",
}


@dataclass(slots=True)
class PendingConfirmation:
    confirmation_id: int
    goal: str
    plan: TaskPlan
    prompt: str
    expires_at: float
    required_approvals: int = 1
    approvals_received: int = 0


@dataclass(slots=True)
class ConfirmationResolution:
    matched: bool
    approved: bool
    response: str
    plan: TaskPlan | None = None
    goal: str = ""


class ConfirmationHandler:
    def __init__(self, timeout_seconds: float = 60.0) -> None:
        self._timeout_seconds = timeout_seconds
        self._pending: PendingConfirmation | None = None
        self._active_approvals: set[tuple[str, str]] = set()
        self._confirmation_counter = 0

    def queue(self, goal: str, plan: TaskPlan, *, prompt: str | None = None) -> str:
        self.expire_pending()
        prompt = prompt or self._build_prompt(goal, plan)
        self._confirmation_counter += 1
        required_approvals = self._required_approvals(plan)
        self._pending = PendingConfirmation(
            confirmation_id=self._confirmation_counter,
            goal=goal,
            plan=plan,
            prompt=prompt,
            expires_at=time.monotonic() + self._timeout_seconds,
            required_approvals=required_approvals,
        )
        return prompt

    def resolve(self, user_input: str) -> ConfirmationResolution | None:
        normalized = self._normalize(user_input)
        if self._pending is None:
            if normalized in _AFFIRMATIVE_REPLIES or normalized in _NEGATIVE_REPLIES:
                return ConfirmationResolution(
                    matched=True,
                    approved=False,
                    response="There is nothing waiting for confirmation.",
                )
            return None

        expired = self.expire_pending()
        if expired:
            if normalized in _AFFIRMATIVE_REPLIES or normalized in _NEGATIVE_REPLIES:
                return ConfirmationResolution(
                    matched=True,
                    approved=False,
                    goal=expired,
                    response=f"Confirmation expired for '{expired}'. Please ask again.",
                )
            return None

        if normalized in _NEGATIVE_REPLIES:
            goal = self._pending.goal
            self._pending = None
            return ConfirmationResolution(
                matched=True,
                approved=False,
                goal=goal,
                response="Okay, I cancelled that request.",
            )

        if normalized in _AFFIRMATIVE_REPLIES:
            self._pending.approvals_received += 1
            if self._pending.approvals_received < self._pending.required_approvals:
                remaining = self._pending.required_approvals - self._pending.approvals_received
                return ConfirmationResolution(
                    matched=True,
                    approved=False,
                    goal=self._pending.goal,
                    response=(
                        "High-risk request detected. "
                        f"Please confirm again to continue ({remaining} confirmation left)."
                    ),
                )

            approved = self._pending.plan
            goal = self._pending.goal
            self.activate(approved)
            self._pending = None
            return ConfirmationResolution(
                matched=True,
                approved=True,
                plan=approved,
                goal=goal,
                response="",
            )
        return None

    def has_pending(self) -> bool:
        self.expire_pending()
        return self._pending is not None

    def clear_pending(self) -> None:
        self._pending = None

    def expire_pending(self) -> str:
        if self._pending is None:
            return ""
        if time.monotonic() <= self._pending.expires_at:
            return ""
        expired_goal = self._pending.goal
        self._pending = None
        return expired_goal

    def activate(self, plan: TaskPlan) -> None:
        self._active_approvals = {
            (step.action, self._normalize(step.description))
            for step in plan.steps
            if self._step_requires_confirmation(step.action, step.risk_level)
        }

    def deactivate(self) -> None:
        self._active_approvals.clear()

    def confirm_action(self, action_name: str, description: str) -> bool | None:
        normalized_description = self._normalize(description)
        if (action_name, normalized_description) in self._active_approvals:
            return True
        return None

    def _build_prompt(self, goal: str, plan: TaskPlan) -> str:
        risky_steps = [
            step for step in plan.steps if self._step_requires_confirmation(step.action, step.risk_level)
        ]
        required_approvals = self._required_approvals(plan)
        if len(risky_steps) == 1:
            step = risky_steps[0]
            validation_reason = str(step.params.get("_validation_reason", "")).strip()
            if required_approvals > 1:
                target = step.target or "that target"
                return (
                    f"High-risk warning: {validation_reason or 'this action affects a broad scope.'} "
                    f"Do you want me to {self._friendly_action(step.action)} {target}? "
                    "Reply yes twice to continue."
                )
            if step.target:
                return f"Are you sure you want me to {self._friendly_action(step.action)} {step.target}?"
            description = step.description.rstrip(".")
            return f"Are you sure you want me to {description.lower()}?"
        return (
            f"Are you sure you want me to execute {len(risky_steps)} sensitive steps "
            f"for '{goal}'?"
        )

    @staticmethod
    def _required_approvals(plan: TaskPlan) -> int:
        strongest = 1
        for step in plan.steps:
            strongest = max(strongest, int(step.params.get("_confirmation_level", 1) or 1))
        return strongest

    @staticmethod
    def _step_requires_confirmation(action_name: str, risk_level: RiskLevel | str) -> bool:
        lowered_risk = risk_level.value if hasattr(risk_level, "value") else str(risk_level).lower()
        return action_name in {"delete_file", "overwrite_file", "install_app", "system_action"} or lowered_risk in {
            RiskLevel.HIGH.value,
            RiskLevel.CRITICAL.value,
        }

    @staticmethod
    def _friendly_action(action_name: str) -> str:
        return {
            "delete_file": "delete",
            "overwrite_file": "overwrite",
            "install_app": "install",
            "system_action": "run system action",
        }.get(action_name, action_name.replace("_", " "))

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.strip().lower().split())
