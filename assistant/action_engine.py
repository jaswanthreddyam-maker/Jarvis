from __future__ import annotations

import logging
from typing import Any

from assistant.actions.base import ActionContext
from assistant.actions.registry import ActionRegistry
from assistant.cancellation_controller import CancelledError, CancellationController
from assistant.contracts import (
    ActionResult,
    ExecutionFeedback,
    FeedbackLevel,
    StepState,
)
from assistant.event_bus import EventBus
from assistant.memory.memory import MemoryManager
from assistant.permissions import PermissionManager
from assistant.scheduler import Scheduler
from assistant.scope_validator import ActionScopeValidator
from assistant.settings import Settings

logger = logging.getLogger("Jarvis.ActionEngine")


class ActionEngine:
    """
    Executes individual action steps with permission checks and execution feedback.
    """

    def __init__(
        self,
        registry: ActionRegistry,
        permissions: PermissionManager,
        event_bus: EventBus,
        memory: MemoryManager,
        scheduler: Scheduler,
        settings: Settings,
        observer: object | None = None,
        cancellation_controller: CancellationController | None = None,
        scope_validator: ActionScopeValidator | None = None,
    ) -> None:
        self._registry = registry
        self._permissions = permissions
        self._event_bus = event_bus
        self._cancellation_controller = cancellation_controller
        self._scope_validator = scope_validator or ActionScopeValidator()
        self._context = ActionContext(
            memory=memory,
            scheduler=scheduler,
            settings=settings,
            event_bus=event_bus,
            observer=observer,
        )

    def execute(
        self,
        step: StepState,
        *,
        task_id: str = "",
        step_index: int = 0,
        step_total: int = 1,
        shared_context: dict[str, Any] | None = None,
    ) -> ActionResult:
        """
        Execute a single step, gated by permissions and wrapped in feedback events.
        """
        token = self._cancellation_controller.current_token if self._cancellation_controller else None
        self._context.cancellation_token = token
        self._context.shared_context = dict(shared_context or {})
        if token is not None and token.is_cancelled():
            return self._cancelled_result(step)

        assessment = self._scope_validator.assess(step.action, step.target, step.params)
        step.params.setdefault("_scope", assessment.scope)
        step.params.setdefault("_safety_level", assessment.safety_level)
        step.params.setdefault("_confirmation_level", assessment.confirmation_level)
        step.params.setdefault("_validation_reason", assessment.reason)
        step.params.setdefault("_estimated_items", assessment.estimated_items)

        if not assessment.valid:
            blocked_message = self._format_execution_message(
                "BLOCKED",
                assessment.reason,
            )
            self._emit_feedback(
                step,
                task_id,
                step_index,
                step_total,
                FeedbackLevel.BLOCKED,
                blocked_message,
            )
            self._emit_safety_status(
                task_id,
                step,
                safety_level="BLOCKED",
                reason=assessment.reason,
                scope=assessment.scope,
            )
            return ActionResult(
                success=False,
                message=blocked_message,
                error="validation_blocked",
                data={
                    "execution_state": "blocked",
                    "scope": assessment.scope,
                    "safety_level": "BLOCKED",
                    "validation_reason": assessment.reason,
                },
            )

        decision = self._permissions.decision_for(
            step.action,
            risk_level=assessment.risk_level.value if hasattr(assessment.risk_level, "value") else step.risk_level,
            description=step.description,
            safe_mode=self._context.settings.safe_mode,
        )
        effective_safety = "CONFIRMATION REQUIRED" if assessment.requires_confirmation else decision.safety_level
        self._emit_safety_status(
            task_id,
            step,
            safety_level=effective_safety,
            reason=assessment.reason if assessment.requires_confirmation else decision.reason,
            scope=assessment.scope,
        )

        if decision.status in {"deny", "sandbox_only"}:
            blocked_message = self._format_execution_message(
                "BLOCKED",
                f"Blocked by policy: {decision.reason}",
            )
            self._emit_feedback(
                step,
                task_id,
                step_index,
                step_total,
                FeedbackLevel.BLOCKED,
                blocked_message,
            )
            self._event_bus.publish(
                "permission.blocked",
                {
                    "action": step.action,
                    "reason": decision.reason,
                    "category": decision.action_category,
                },
            )
            self._log_action_state(step, "BLOCKED", decision.reason)
            return ActionResult(
                success=False,
                message=blocked_message,
                error="permission_blocked",
                data={
                    "status": decision.status,
                    "action_category": decision.action_category,
                    "execution_state": "blocked",
                    "scope": assessment.scope,
                    "safety_level": "BLOCKED",
                    "validation_reason": assessment.reason,
                },
            )

        if decision.status == "confirm_required":
            blocked_message = self._format_execution_message(
                "BLOCKED",
                f"Requires confirmation: {decision.reason}",
            )
            self._emit_feedback(
                step,
                task_id,
                step_index,
                step_total,
                FeedbackLevel.BLOCKED,
                blocked_message,
            )
            self._event_bus.publish(
                "permission.confirm",
                {
                    "action": step.action,
                    "reason": decision.reason,
                    "category": decision.action_category,
                },
            )
            self._log_action_state(step, "BLOCKED", decision.reason)
            return ActionResult(
                success=False,
                message=blocked_message,
                error="confirmation_required",
                data={
                    "status": decision.status,
                    "needs_confirmation": True,
                    "action_category": decision.action_category,
                    "execution_state": "blocked",
                    "scope": assessment.scope,
                    "safety_level": "CONFIRMATION REQUIRED",
                    "validation_reason": assessment.reason,
                },
            )

        if step.action == "system_action" and decision.needs_confirmation:
            step.params["_confirmed_system_action"] = True

        self._emit_feedback(
            step,
            task_id,
            step_index,
            step_total,
            FeedbackLevel.INFO,
            f"Executing: {step.description or step.action}",
        )
        self._event_bus.publish(
            "action.started",
            {"action": step.action, "params": step.params},
        )

        try:
            result = self._registry.call(step.action, step.params, self._context)
        except CancelledError as exc:
            result = ActionResult(
                success=False,
                message=str(exc) or f"Cancelled {step.action}.",
                error="cancelled",
                data={"execution_state": "cancelled"},
            )

        result.data.setdefault("action", step.action)
        result.data.setdefault("target", step.target)
        result.data.setdefault("action_category", decision.action_category)
        result.data.setdefault("scope", assessment.scope)
        result.data.setdefault("safety_level", effective_safety if result.success else decision.safety_level)
        result.data.setdefault("validation_reason", assessment.reason)
        result.data.setdefault(
            "execution_state",
            "simulated" if result.data.get("simulated") else "executed",
        )
        result.data.setdefault("verified", False)
        self._attach_runtime_observation(step, result)

        if result.success:
            execution_label = "SIMULATED" if result.data.get("simulated") else "EXECUTED"
            self._emit_feedback(
                step,
                task_id,
                step_index,
                step_total,
                FeedbackLevel.SUCCESS,
                self._format_execution_message(execution_label, result.message),
            )
            self._event_bus.publish(
                "action.completed",
                {
                    "action": step.action,
                    "message": result.message,
                    "category": decision.action_category,
                    "execution_state": result.data.get("execution_state"),
                },
            )
            self._log_action_state(step, execution_label, result.message)
        else:
            self._emit_feedback(
                step,
                task_id,
                step_index,
                step_total,
                FeedbackLevel.ERROR,
                result.message,
            )
            self._event_bus.publish(
                "action.failed",
                {"action": step.action, "message": result.message, "error": result.error},
            )
            self._log_action_state(step, "ERROR", result.message)

        return result

    @staticmethod
    def _cancelled_result(step: StepState) -> ActionResult:
        return ActionResult(
            success=False,
            message=f"Cancelled {step.description or step.action}.",
            error="cancelled",
            data={"execution_state": "cancelled"},
        )

    def _emit_safety_status(
        self,
        task_id: str,
        step: StepState,
        *,
        safety_level: str,
        reason: str,
        scope: str,
    ) -> None:
        self._event_bus.publish(
            "safety.status",
            {
                "task_id": task_id,
                "step_id": step.step_id,
                "action": step.action,
                "description": step.description,
                "safety_level": safety_level,
                "reason": reason,
                "scope": scope,
            },
        )

    def _emit_feedback(
        self,
        step: StepState,
        task_id: str,
        step_index: int,
        step_total: int,
        level: FeedbackLevel,
        message: str,
    ) -> None:
        """Publish an ExecutionFeedback event for UI consumption."""
        feedback = ExecutionFeedback(
            task_id=task_id,
            step_index=step_index,
            step_total=step_total,
            action=step.action,
            description=step.description,
            level=level,
            message=message,
        )
        self._event_bus.publish("execution.feedback", {"feedback": feedback})
        logger.info(
            "[Step %d/%d] [%s] %s - %s",
            step_index + 1,
            step_total,
            level.value.upper(),
            step.action,
            message,
        )

    def _log_action_state(self, step: StepState, state: str, message: str) -> None:
        detail = step.target or self._extract_detail(message)
        logger.info("[Jarvis][%s] %s -> %s", state, step.action, detail)

    @staticmethod
    def _extract_detail(message: str) -> str:
        cleaned = " ".join(message.split())
        return cleaned[:180]

    @staticmethod
    def _format_execution_message(state: str, message: str) -> str:
        return f"[{state}] {message}"

    def _attach_runtime_observation(self, step: StepState, result: ActionResult) -> None:
        observer = getattr(self._context, "observer", None)
        if observer is None or not hasattr(observer, "capture"):
            return
        try:
            observation = observer.capture(
                action=step.action,
                result_data=result.data,
                expected_state={},
                simulated=bool(result.data.get("simulated")),
            )
        except Exception as exc:
            logger.debug("Runtime observation failed for %s: %s", step.action, exc)
            return

        if not isinstance(observation, dict):
            return
        result.data["runtime_observation"] = observation
        observed_window = str(observation.get("window_title", "")).strip()
        if observed_window:
            result.data.setdefault("window_title", observed_window)
        observed_app = str(observation.get("active_app", "")).strip()
        if observed_app:
            result.data.setdefault("observed_active_app", observed_app)
        observed_browser = str(observation.get("browser_app", "")).strip()
        if observed_browser:
            result.data.setdefault("observed_browser_app", observed_browser)
        observed_url = str(observation.get("url", "")).strip()
        if observed_url:
            result.data.setdefault("observed_url", observed_url)
        playback_state = str(observation.get("playback_state", "")).strip()
        if playback_state:
            result.data.setdefault("observed_playback_state", playback_state)
