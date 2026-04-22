from __future__ import annotations

import logging
import math
import threading
from collections import deque
from typing import Any

from assistant.action_engine import ActionEngine
from assistant.cancellation_controller import CancellationController
from assistant.contracts import ActionResult, StepState, TaskPlan, TaskState
from assistant.error_handler import ErrorHandler
from assistant.event_bus import EventBus, Events
from assistant.execution_finalizer import ExecutionFinalizer
from assistant.memory.memory import MemoryManager
from assistant.personality import Personality
from assistant.scheduler import Scheduler
from assistant.state_manager import TaskStateManager

logger = logging.getLogger("Jarvis.Orchestrator")


class Orchestrator:
    """
    Central orchestrator with separated planner and executor flow.

    Key features:
        - Planner produces a TaskPlan; executor runs it step-by-step.
        - Interrupt-safe: call `interrupt()` to stop mid-sequence cleanly.
        - Per-step execution feedback published to the event bus.
        - Tracks success/failure and updates workflow memory automatically.
    """

    def __init__(
        self,
        planner,
        action_engine: ActionEngine,
        state_manager: TaskStateManager,
        error_handler: ErrorHandler,
        event_bus: EventBus,
        scheduler: Scheduler,
        memory: MemoryManager,
        personality: Personality,
        cancellation_controller: CancellationController | None = None,
        execution_finalizer: ExecutionFinalizer | None = None,
    ) -> None:
        self._planner = planner
        self._action_engine = action_engine
        self._state_manager = state_manager
        self._error_handler = error_handler
        self._event_bus = event_bus
        self._scheduler = scheduler
        self._memory = memory
        self._personality = personality
        self._cancellation_controller = cancellation_controller or CancellationController()
        self._execution_finalizer = execution_finalizer or ExecutionFinalizer()
        self._notifications: deque[str] = deque()
        self._interrupted = threading.Event()
        self._active_task_id = ""
        self.is_busy = False
        self._event_bus.subscribe("reminder.due", self._on_reminder_due)


    def interrupt(self) -> None:
        """Signal the orchestrator to stop executing after the current step."""
        self._interrupted.set()
        self._cancellation_controller.cancel()
        if self._active_task_id:
            self._event_bus.publish(
                "execution.cancel_requested",
                {"task_id": self._active_task_id},
            )
        logger.info("Orchestrator interrupt requested.")

    def reset_interrupt(self) -> None:
        """Clear the interrupt flag so the next task can run normally."""
        self._interrupted.clear()

    @property
    def is_interrupted(self) -> bool:
        return self._interrupted.is_set()

    def handle_text(self, user_input: str) -> tuple[str, TaskState | None]:
        if self.is_busy:
            return "Assistant is busy.", None

        self.is_busy = True
        try:
            self.reset_interrupt()
            plan = self._create_plan(user_input)
            task = self._state_manager.create_task(user_input, plan)
            self._active_task_id = task.task_id
            self._event_bus.publish(
                "execution.plan_created",
                {
                    "task_id": task.task_id,
                    "intent": plan.intent,
                    "step_count": len(plan.steps),
                    "reused_from_memory": plan.reused_from_memory,
                },
            )
            return self._execute_task_plan(task, plan, user_input)
        except Exception as exc:
            logger.exception("Orchestrator failed while handling '%s'", user_input)
            return f"I couldn't complete that request. {exc}", None

        finally:
            self.is_busy = False

    def _execute_task_plan(self, task, plan, goal_text) -> tuple[str, TaskState]:
        """Extracted logic for executing a task plan sequentially."""
        if not task.steps:
            response = self._personality.format_response(
                plan.fallback_response or "I need more detail."
            )
            self._state_manager.set_final_response(task.task_id, response)
            self._state_manager.mark_task_completed(task.task_id)
            self._memory.add_turn(goal_text, response)
            self._active_task_id = ""
            return response, task

        self._state_manager.mark_task_running(task.task_id)
        messages: list[str] = []
        had_failure = False
        was_interrupted = False
        failed_step: StepState | None = None
        failed_result = ActionResult(success=False, message="Plan failed.", error="plan_failed")
        fallback_attempted = False
        fallback_succeeded = False
        total_steps = len(task.steps)
        execution_context: dict[str, Any] = {}
        from assistant.task_context import task_context

        task_context.start_workflow()

        if hasattr(self._planner, "start_execution"):
            self._planner.start_execution(plan)

        try:


            for step_index, step in enumerate(task.steps):
                if self._interrupted.is_set() or self._is_cancelled():
                    was_interrupted = True
                    self._state_manager.mark_step_skipped(
                        task.task_id, step.step_id, "Interrupted by user."
                    )
                    self._event_bus.publish(
                        "execution.step_skipped",
                        {
                            "task_id": task.task_id,
                            "step_id": step.step_id,
                            "reason": "Interrupted by user.",
                        },
                    )
                    for remaining in task.steps[step_index + 1 :]:
                        self._state_manager.mark_step_skipped(
                            task.task_id, remaining.step_id, "Skipped due to interrupt."
                        )
                    messages.append("Execution interrupted by user.")
                    break

                self._event_bus.publish(
                    "execution.step_start",
                    {
                        "task_id": task.task_id,
                        "step_id": step.step_id,
                        "step_index": step_index,
                        "step_total": total_steps,
                        "action": step.action,
                        "description": step.description,
                    },
                )

                max_attempts = max(
                    self._error_handler.max_attempts_for(step.action),
                    step.max_retries + 1,
                )
                step_result = ActionResult(
                    success=False,
                    message="Action did not run.",
                    error="no_attempt",
                )

                for attempt in range(max_attempts):
                    dependency_failure = self._bind_step_context(step, execution_context)
                    if dependency_failure is not None:
                        step_result = dependency_failure
                        self._state_manager.mark_step_failed(task.task_id, step.step_id, step_result)
                        break

                    self._state_manager.mark_step_running(task.task_id, step.step_id)
                    result = self._action_engine.execute(
                        step,
                        task_id=task.task_id,
                        step_index=step_index,
                        step_total=total_steps,
                        shared_context=task_context.workflow_snapshot(),
                    )
                    intent_payload = {
                        "intent": step.action,
                        "target": step.target,
                        "params": dict(step.params),
                    }

                    if result.success:
                        verified, verification_message = self._verify_step(step, result)
                        if verified:
                            result.data["verified"] = True
                            result.data["goal_progress"] = {
                                "completed_steps": step_index + 1,
                                "total_steps": total_steps,
                                "current_action": step.action,
                                "target_state": dict(plan.goal_state),
                            }
                            task_context.record_result(
                                intent=intent_payload,
                                result=task_context.build_result_contract(
                                    success=True,
                                    action=step.action,
                                    target=step.target,
                                    data=result.data,
                                    verified=True,
                                ),
                            )
                            self._state_manager.mark_step_completed(
                                task.task_id, step.step_id, result
                            )
                            self._event_bus.publish(
                                "execution.step_done",
                                {
                                    "task_id": task.task_id,
                                    "step_id": step.step_id,
                                    "message": result.message,
                                },
                            )
                            messages.append(result.message)
                            step_result = result
                            execution_context[f"step_{step.plan_step_id}"] = {
                                "action": step.action,
                                "target": step.target,
                                "data": dict(result.data),
                                "message": result.message,
                            }
                            break

                        result = ActionResult(
                            success=False,
                            message=f"{result.message} Verification failed: {verification_message}",
                            error="verification_failed",
                            data={**dict(result.data), "verified": False},
                        )

                    result.data["verified"] = False
                    task_context.record_result(
                        intent=intent_payload,
                        result=task_context.build_result_contract(
                            success=False,
                            action=step.action,
                            target=step.target,
                            data=result.data,
                            error=result.error,
                            verified=False,
                        ),
                    )
                    step_result = result
                    self._state_manager.mark_step_failed(task.task_id, step.step_id, result)
                    if attempt < max_attempts - 1:
                        logger.info(
                            "Retrying step '%s' (attempt %d/%d)",
                            step.action,
                            attempt + 2,
                            max_attempts,
                        )

                if step_result.success:
                    continue

                if step_result.error == "cancelled":
                    was_interrupted = True
                    messages.append(step_result.message)
                    for remaining in task.steps[step_index + 1 :]:
                        self._state_manager.mark_step_skipped(
                            task.task_id,
                            remaining.step_id,
                            "Skipped due to interrupt.",
                        )
                    break

                fallback_attempted = bool(step.fallback_action) and not bool(
                    step_result.data.get("rollback_attempted")
                )
                fallback_succeeded = False
                if fallback_attempted:
                    primary_failure = step_result
                    fallback_result = self._run_fallback_step(
                        task_id=task.task_id,
                        step=step,
                        step_index=step_index,
                        step_total=total_steps,
                    )
                    if fallback_result.success:
                        fallback_succeeded = True
                        fallback_result.data["verified"] = True
                        task_context.record_result(
                            intent={
                                "intent": step.fallback_action or step.action,
                                "target": step.fallback_target or step.target,
                                "params": dict(step.fallback_params or {}),
                            },
                            result=task_context.build_result_contract(
                                success=True,
                                action=step.fallback_action or step.action,
                                target=step.fallback_target or step.target,
                                data=fallback_result.data,
                                verified=True,
                            ),
                        )
                        completed_result = ActionResult(
                            success=True,
                            message=f"Fallback succeeded: {fallback_result.message}",
                            data={**fallback_result.data, "fallback_used": True},
                        )
                        self._state_manager.mark_step_completed(
                            task.task_id, step.step_id, completed_result
                        )
                        self._event_bus.publish(
                            "execution.step_done",
                            {
                                "task_id": task.task_id,
                                "step_id": step.step_id,
                                "message": completed_result.message,
                            },
                        )
                        messages.append(completed_result.message)
                        execution_context[f"step_{step.plan_step_id}"] = {
                            "action": step.fallback_action or step.action,
                            "target": step.fallback_target or step.target,
                            "data": dict(completed_result.data),
                            "message": completed_result.message,
                        }
                        continue
                    step_result = ActionResult(
                        success=False,
                        message=(
                            f"{primary_failure.message}\n"
                            f"Fallback failed: {fallback_result.message}"
                        ),
                        error=primary_failure.error or fallback_result.error,
                        data={**primary_failure.data, "fallback_error": fallback_result.message},
                    )

                had_failure = True
                failed_step = step
                failed_result = step_result
                messages.append(step_result.message)
                self._event_bus.publish(
                    "execution.step_failed",
                    {
                        "task_id": task.task_id,
                        "step_id": step.step_id,
                        "error": step_result.error,
                        "message": step_result.message,
                    },
                )
                for remaining in task.steps[step_index + 1 :]:
                    self._state_manager.mark_step_skipped(
                        task.task_id,
                        remaining.step_id,
                        f"Skipped due to failure in step '{step.action}'.",
                    )
                break
        finally:
            if hasattr(self._planner, "finish_execution"):
                self._planner.finish_execution()
            self._active_task_id = ""

        if was_interrupted:
            self._state_manager.mark_task_interrupted(task.task_id)
            finalized = self._execution_finalizer.finalize(
                self._state_manager.get_task(task.task_id),
                self._compose_response(messages),
            )
            response = self._personality.format_response(finalized.response)
            self._state_manager.set_final_response(task.task_id, response)
            self._event_bus.publish("execution.interrupted", {"task_id": task.task_id})
        elif had_failure:
            completed_task = self._state_manager.get_task(task.task_id)
            used_simulation = self._task_used_simulation(completed_task, task_context.workflow_snapshot()["workflow"])
            if failed_step is not None and hasattr(self._planner, "build_failure_report"):
                failure_response = self._planner.build_failure_report(
                    goal_text,
                    failed_step,
                    failed_result,
                    plan=plan,
                    fallback_attempted=fallback_attempted,
                    fallback_succeeded=fallback_succeeded,
                )
            else:
                failure_response = self._compose_response(messages)
            finalized = self._execution_finalizer.finalize(
                self._state_manager.get_task(task.task_id),
                failure_response,
            )
            response = self._personality.format_response(finalized.response)
            self._state_manager.set_final_response(task.task_id, response)
            self._state_manager.mark_task_failed(task.task_id, response)
            from assistant.behavior_learning import learner

            if not used_simulation:
                learner.record_feedback(goal_text, success=False)
            if hasattr(self._planner, "record_strategy_outcome") and not used_simulation:
                failure_progress = self._failure_progress_from_step_failure(
                    plan,
                    failed_step,
                    failed_result,
                )
                failure_progress["execution_cost"] = self._task_execution_cost(
                    self._state_manager.get_task(task.task_id),
                    plan=plan,
                )
                failure_progress["parser_confidence"] = float(dict(plan.goal_state).get("parser_confidence", 1.0) or 0.0)
                failure_progress["stability_mode"] = str(dict(plan.goal_state).get("stability_mode", "adaptive"))
                self._planner.record_strategy_outcome(
                    goal_text,
                    plan,
                    success=False,
                    progress=failure_progress,
                )
            if hasattr(self._planner, "remember_failed_workflow") and not used_simulation:
                self._planner.remember_failed_workflow(goal_text, self._plan_to_workflow_steps(plan))
            if not used_simulation:
                self._update_memory_graph(goal_text, "failed", plan)
        else:
            workflow_snapshot = task_context.workflow_snapshot()
            completed_task = self._state_manager.get_task(task.task_id)
            used_simulation = self._task_used_simulation(completed_task, workflow_snapshot["workflow"])
            goal_achieved = True
            goal_message = ""
            if hasattr(self._planner, "check_goal_completion"):
                goal_achieved, goal_message = self._planner.check_goal_completion(
                    plan,
                    task=completed_task,
                    workflow_snapshot=workflow_snapshot,
                )

            if not goal_achieved:
                final_progress = {}
                if hasattr(self._planner, "assess_goal_progress"):
                    final_progress = self._planner.assess_goal_progress(
                        plan,
                        task=completed_task,
                        workflow_snapshot=workflow_snapshot,
                    )
                final_progress["execution_cost"] = self._task_execution_cost(
                    self._state_manager.get_task(task.task_id),
                    plan=plan,
                )
                final_progress["parser_confidence"] = float(dict(plan.goal_state).get("parser_confidence", 1.0) or 0.0)
                final_progress["stability_mode"] = str(dict(plan.goal_state).get("stability_mode", "adaptive"))
                if hasattr(self._planner, "record_strategy_outcome") and not used_simulation:
                    self._planner.record_strategy_outcome(
                        goal_text,
                        plan,
                        success=False,
                        progress=final_progress,
                    )
                self._event_bus.publish(
                    "execution.goal_progress",
                    {"task_id": task.task_id, "progress": final_progress},
                )
                recovery_decision = None
                if hasattr(self._planner, "plan_goal_recovery"):
                    recovery_decision = self._planner.plan_goal_recovery(
                        goal_text,
                        plan,
                        final_progress,
                    )
                failure_message = goal_message or "I completed the steps, but I could not confirm that the goal was achieved."
                if recovery_decision and str(recovery_decision.get("mode", "")).strip().lower() == "auto_execute":
                    failure_message = str(
                        recovery_decision.get(
                            "message",
                            "The primary goal was not confirmed, so I'm attempting a safe recovery automatically.",
                        )
                    )
                elif recovery_decision and str(recovery_decision.get("mode", "")).strip().lower() == "confirm":
                    failure_message = f"{failure_message} {recovery_decision['prompt']}"
                elif recovery_decision and str(recovery_decision.get("mode", "")).strip().lower() in {"limit_reached", "blocked"}:
                    failure_message = f"{failure_message} {recovery_decision['message']}"
                elif hasattr(self._planner, "build_goal_failure_response"):
                    failure_message = self._planner.build_goal_failure_response(
                        goal_text,
                        plan,
                        final_progress,
                        failure_message,
                    )
                response = self._personality.format_response(
                    failure_message
                )
                self._state_manager.set_final_response(task.task_id, response)
                self._state_manager.mark_task_failed(task.task_id, response)
                self._event_bus.publish(
                    "execution.goal_incomplete",
                    {"task_id": task.task_id, "response": response},
                )
                from assistant.behavior_learning import learner

                if not used_simulation:
                    learner.record_feedback(goal_text, success=False)
                if hasattr(self._planner, "remember_failed_workflow") and not used_simulation:
                    self._planner.remember_failed_workflow(goal_text, self._plan_to_workflow_steps(plan))
                if not used_simulation:
                    self._update_memory_graph(goal_text, "failed", plan)
                if recovery_decision and str(recovery_decision.get("mode", "")).strip().lower() == "auto_execute":
                    self._publish_finalized_event(task.task_id, response)
                    return self._execute_recovery_task(
                        goal_text,
                        recovery_decision=recovery_decision,
                    )
            else:
                final_progress = {}
                if hasattr(self._planner, "assess_goal_progress"):
                    final_progress = self._planner.assess_goal_progress(
                        plan,
                        task=completed_task,
                        workflow_snapshot=workflow_snapshot,
                    )
                    self._event_bus.publish(
                        "execution.goal_progress",
                        {
                            "task_id": task.task_id,
                            "progress": final_progress,
                        },
                    )
                final_progress["execution_cost"] = self._task_execution_cost(
                    self._state_manager.get_task(task.task_id),
                    plan=plan,
                )
                final_progress["parser_confidence"] = float(dict(plan.goal_state).get("parser_confidence", 1.0) or 0.0)
                final_progress["stability_mode"] = str(dict(plan.goal_state).get("stability_mode", "adaptive"))
                if hasattr(self._planner, "record_strategy_outcome") and not used_simulation:
                    self._planner.record_strategy_outcome(
                        goal_text,
                        plan,
                        success=True,
                        progress=final_progress,
                    )
                workflow_steps = workflow_snapshot["workflow"]
                if hasattr(self._planner, "remember_successful_workflow") and self._should_learn_successful_workflow(
                    workflow_steps
                ):
                    self._planner.remember_successful_workflow(goal_text, workflow_steps)
                if hasattr(self._planner, "remember_successful_plan") and not used_simulation:
                    self._planner.remember_successful_plan(goal_text, plan)
                elif hasattr(self._planner, "remember_session_plan") and used_simulation:
                    self._planner.remember_session_plan(goal_text, plan)
                if hasattr(self._planner, "build_success_response"):
                    response = self._personality.format_response(
                        self._planner.build_success_response(goal_text, plan, messages)
                    )
                else:
                    response = self._personality.format_response(self._compose_response(messages))
                self._state_manager.set_final_response(task.task_id, response)
                self._state_manager.mark_task_completed(task.task_id)
                self._event_bus.publish(
                    "execution.completed",
                    {"task_id": task.task_id, "response": response},
                )
                from assistant.behavior_learning import learner

                if not used_simulation:
                    learner.record_feedback(goal_text, success=True)
                    self._update_memory_graph(goal_text, "success", plan)

        self._publish_finalized_event(task.task_id, response)

        self._memory.add_turn(goal_text, response)
        self._collect_due_notifications()
        return response, task



    def poll_notifications(self) -> list[str]:
        self._collect_due_notifications()
        items = list(self._notifications)
        self._notifications.clear()
        return items

    def _create_plan(self, user_input: str) -> TaskPlan:
        if hasattr(self._planner, "plan"):
            return self._planner.plan(user_input)
        if hasattr(self._planner, "generate_plan"):
            return self._planner.generate_plan(user_input)
        raise TypeError("Planner does not expose a supported planning method.")

    @staticmethod
    def _compose_response(messages: list[str]) -> str:
        cleaned = [message.strip() for message in messages if message.strip()]
        if not cleaned:
            return "The request completed without a detailed response."
        return "\n".join(cleaned)

    def _verify_step(self, step: StepState, result: ActionResult) -> tuple[bool, str]:
        if hasattr(self._planner, "verify_step"):
            return self._planner.verify_step(step, result)
        return result.success, result.message

    def _run_fallback_step(
        self,
        *,
        task_id: str,
        step: StepState,
        step_index: int,
        step_total: int,
    ) -> ActionResult:
        fallback_step = StepState(
            step_id=f"{task_id}-step-{step.plan_step_id}-fallback",
            plan_step_id=step.plan_step_id,
            action=step.fallback_action,
            target=step.fallback_target or step.target,
            params=dict(step.fallback_params),
            description=step.fallback_description or f"Fallback for {step.description}",
            verification=step.fallback_verification,
            retry_group=step.retry_group,
            risk_level=step.risk_level,
            expected_window=step.expected_window,
            max_retries=0,
        )
        result = self._action_engine.execute(
            fallback_step,
            task_id=task_id,
            step_index=step_index,
            step_total=step_total,
        )
        if not result.success:
            return result

        verified, verification_message = self._verify_step(fallback_step, result)
        if verified:
            result.data["verified"] = True
            return result
        return ActionResult(
            success=False,
            message=f"{result.message} Verification failed: {verification_message}",
            error="verification_failed",
            data={**dict(result.data), "verified": False},
        )

    def _collect_due_notifications(self) -> None:
        self._scheduler.drain_due()

    def _bind_step_context(
        self,
        step: StepState,
        execution_context: dict[str, Any],
    ) -> ActionResult | None:
        for dependency in step.depends_on:
            if f"step_{dependency}" not in execution_context:
                return ActionResult(
                    success=False,
                    message=f"Step {step.plan_step_id} is waiting on step {dependency}.",
                    error="dependency_missing",
                )

        for param_name, source in step.param_bindings.items():
            resolved = self._resolve_context_value(execution_context, source)
            if resolved in {None, ""}:
                return ActionResult(
                    success=False,
                    message=f"Step {step.plan_step_id} could not resolve {param_name}.",
                    error="dependency_missing",
                )
            step.params[param_name] = resolved
        return None

    @staticmethod
    def _resolve_context_value(execution_context: dict[str, Any], source: str) -> Any:
        current: Any = execution_context
        for part in source.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
                continue
            return None
        return current

    @staticmethod
    def _failure_progress_from_step_failure(
        plan: TaskPlan,
        failed_step: StepState | None,
        failed_result: ActionResult,
    ) -> dict[str, Any]:
        action = str(getattr(failed_step, "action", "")).strip().lower()
        error = str(failed_result.error or failed_result.status or "").strip().lower()
        failure_reason = "strategy_failure"
        failure_type = "strategy_failure"
        if error in {"verification_failed", "not_found"} and action in {
            "open_app",
            "open_url",
            "search_web",
            "search_youtube",
            "play_youtube",
            "focus_app",
            "close_app",
        }:
            failure_reason = "capability_limit"
            failure_type = "capability_limit"
        elif error in {"permission_blocked", "confirmation_required"} or str(
            failed_result.data.get("execution_state", "")
        ).strip().lower() == "blocked":
            failure_reason = "permission_block"
            failure_type = "permission_block"
        elif error in {"cancelled"}:
            failure_reason = "user_cancelled"
            failure_type = "user_cancelled"

        goal_state = dict(plan.goal_state)
        expected_query = str(goal_state.get("query", "")).strip().lower()
        observed_query = str(
            failed_result.data.get("query")
            or failed_result.data.get("search_query")
            or failed_result.data.get("video_query")
            or ""
        ).strip().lower()
        expected_url = str(goal_state.get("url_contains", "")).strip().lower()
        observed_url = str(
            failed_result.data.get("url")
            or failed_result.data.get("search_url")
            or failed_result.data.get("video_url")
            or ""
        ).strip().lower()
        if expected_query and observed_query and expected_query != observed_query:
            failure_reason = "intent_mismatch"
            failure_type = "intent_mismatch"
        elif expected_url and observed_url and expected_url not in observed_url and error != "not_found":
            failure_reason = "intent_mismatch"
            failure_type = "intent_mismatch"

        return {
            "goal_state": goal_state,
            "completed_steps": max(0, int(getattr(failed_step, "plan_step_id", 1) or 1) - 1),
            "total_steps": len(plan.steps),
            "failed_step_action": action,
            "error": error,
            "failure_reason": failure_reason,
            "failure_type": failure_type,
            "checks": {},
        }

    @staticmethod
    def _task_execution_cost(task: TaskState, *, plan: TaskPlan) -> float:
        action_weights = {
            "open_url": 0.45,
            "search_web": 0.35,
            "search_youtube": 0.35,
            "play_youtube": 0.95,
            "open_app": 0.6,
            "focus_app": 0.3,
            "close_app": 0.3,
            "install_app": 1.25,
            "system_action": 1.4,
        }
        if not getattr(task, "steps", None):
            return sum(action_weights.get(str(step.action).strip().lower(), 0.5) for step in plan.steps)
        total = 0.0
        attempts: list[int] = []
        for step in task.steps:
            action = str(step.action).strip().lower()
            step_attempts = max(1, int(getattr(step, "attempts", 0) or 0))
            attempts.append(step_attempts)
            total += action_weights.get(action, 0.5) * step_attempts

        elapsed_seconds = max(
            0.0,
            (getattr(task, "updated_at", task.created_at) - task.created_at).total_seconds(),
        )
        duration_component = min(1.0, elapsed_seconds / max(1.0, len(task.steps))) * 0.25
        variability_component = 0.0
        if attempts:
            mean_attempts = sum(attempts) / len(attempts)
            variance = sum((value - mean_attempts) ** 2 for value in attempts) / len(attempts)
            variability_component = min(0.35, math.sqrt(variance) * 0.12)
        return total + duration_component + variability_component

    def _publish_finalized_event(self, task_id: str, response: str) -> None:
        self._event_bus.publish(
            "execution.finalized",
            {
                "task_id": task_id,
                "status": self._state_manager.get_task(task_id).status,
                "response": response,
                "steps": [
                    {
                        "action": step.action,
                        "description": step.description,
                        "status": step.status,
                    }
                    for step in self._state_manager.get_task(task_id).steps
                ],
            },
        )

    def _execute_recovery_task(
        self,
        goal_text: str,
        *,
        recovery_decision: dict[str, Any],
    ) -> tuple[str, TaskState]:
        recovery_plan = recovery_decision.get("plan")
        if not isinstance(recovery_plan, TaskPlan):
            raise TypeError("Recovery decision did not include a valid TaskPlan.")

        self._event_bus.publish(
            "execution.recovery_auto",
            {
                "goal": goal_text,
                "strategy_id": recovery_decision.get("strategy_id"),
                "score": recovery_decision.get("score"),
            },
        )
        recovery_task = self._state_manager.create_task(goal_text, recovery_plan)
        self._active_task_id = recovery_task.task_id
        return self._execute_task_plan(recovery_task, recovery_plan, goal_text)

    def _on_reminder_due(self, payload: dict[str, Any]) -> None:
        message = str(payload.get("message", "Reminder due."))
        self._notifications.append(message)

    def _is_cancelled(self) -> bool:
        token = self._cancellation_controller.current_token
        return bool(token and token.is_cancelled())

    def _update_memory_graph(self, user_input: str, result_state: str, plan: TaskPlan) -> None:
        """Record this interaction in the memory graph for pattern learning."""
        try:
            from assistant.memory_graph import graph

            intent_type = "local"
            app_name = None
            if plan and plan.steps:
                intent_type = plan.steps[0].action or "local"
                for step in plan.steps:
                    desc = (step.description or "").lower()
                    for app in [
                        "youtube",
                        "chrome",
                        "vscode",
                        "spotify",
                        "notepad",
                        "terminal",
                        "firefox",
                        "discord",
                        "slack",
                        "teams",
                    ]:
                        if app in desc:
                            app_name = app
                            break
            graph.record_interaction(user_input, intent_type, result_state, app=app_name)
            self._event_bus.publish_async(
                Events.MEMORY_UPDATED,
                {
                    "input": user_input[:50],
                    "state": result_state,
                },
            )
        except Exception as exc:
            logger.debug("Memory graph update failed: %s", exc)

    @staticmethod
    def _plan_to_workflow_steps(plan: TaskPlan) -> list[dict[str, Any]]:
        return [
            {
                "intent": step.action,
                "target": step.target,
                "params": dict(step.params),
            }
            for step in (plan.steps if plan else [])
        ]

    @staticmethod
    def _should_learn_successful_workflow(workflow_steps: list[dict[str, Any]]) -> bool:
        if not workflow_steps:
            return False
        for step in workflow_steps:
            result = dict(step.get("result") or {})
            if bool(result.get("simulated")) or str(result.get("execution_state", "")).strip().lower() == "simulated":
                return False
        return True

    @staticmethod
    def _task_used_simulation(task: TaskState, workflow_steps: list[dict[str, Any]]) -> bool:
        if Orchestrator._contains_simulated_result(workflow_steps):
            return True
        for step in getattr(task, "steps", []):
            if Orchestrator._contains_simulated_result([{"result": dict(getattr(step, "result", {}) or {})}]):
                return True
        return False

    @staticmethod
    def _contains_simulated_result(steps: list[dict[str, Any]]) -> bool:
        for step in steps:
            result = dict(step.get("result") or {})
            if bool(result.get("simulated")) or str(result.get("execution_state", "")).strip().lower() == "simulated":
                return True
        return False
