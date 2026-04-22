from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import subprocess
import time
from typing import Any

from assistant.confirmation_handler import ConfirmationHandler
from assistant.contracts import ActionResult, RiskLevel, StepDefinition, StepState, TaskPlan
from assistant.execution_validator import ExecutionValidator
from assistant.execution_planner import ExecutionPlanner
from assistant.fuzzy_matching import FuzzyAppMatcher, format_app_name
from assistant.intent_session_tracker import IntentSessionTracker
from assistant.memory.memory import MemoryManager
from assistant.runtime_observer import RuntimeObserver
from assistant.session_memory import SessionMemory
from assistant.workflow_memory import WorkflowMemory, workflow_memory
from assistant.workflow_policy import WorkflowPolicy, build_command_context, contexts_match

logger = logging.getLogger("Jarvis.GoalPlanner")

_PLAN_MEMORY_NAMESPACE = "planner"
_PLAN_MEMORY_CATEGORY = "goal_plan"
_STRATEGY_MEMORY_NAMESPACE = "planner_strategies"
_STRATEGY_MEMORY_CATEGORY = "strategy_outcome"
_CONTEXT_STALE_AFTER_SECONDS = 90.0
_MIN_CONTEXT_CONFIDENCE = 0.72
_MAX_RECOVERY_ATTEMPTS = 3
_STRATEGY_EXPLORATION_RATE = 0.18
_CAPABILITY_FAILURE_WEIGHT = 0.25
_AUTO_RECOVERY_CONFIDENCE = 0.5
_EXPLORATION_TIME_WINDOW_SECONDS = 300
_CAPABILITY_BLOCK_THRESHOLD = 2
_USER_REJECTION_FAILURE_WEIGHT = 1.15
_INTENT_MISMATCH_FAILURE_WEIGHT = 1.25
_STRATEGY_COST_WEIGHT = 0.18
_STRATEGY_BLOCK_COOLDOWN_SECONDS = 900.0
_STRATEGY_PENALTY_COOLDOWN_SECONDS = 600.0
_PARSER_MISMATCH_WINDOW_SECONDS = 300.0
_PARSER_CONFIDENCE_FLOOR = 0.50
_STABILITY_WINDOW_RECORDS = 12
_STABILITY_MIN_RECORDS = 6
_STABILITY_FAILURE_RATIO = 0.75
_NEGATIVE_REPLIES = {
    "no",
    "n",
    "cancel",
    "stop",
    "abort",
    "decline",
    "never mind",
    "not that",
    "no not that",
    "nope",
}


class GoalPlanner:
    def __init__(
        self,
        action_engine,
        memory: MemoryManager,
        *,
        execution_planner: ExecutionPlanner | None = None,
        session_memory: SessionMemory | None = None,
        confirmation_handler: ConfirmationHandler | None = None,
        intent_tracker: IntentSessionTracker | None = None,
        workflow_store: WorkflowMemory | None = None,
        observer: RuntimeObserver | None = None,
    ) -> None:
        self._action_engine = action_engine
        self._memory = memory
        self._session_memory = session_memory or SessionMemory()
        self._execution_planner = execution_planner or ExecutionPlanner(
            session_memory=self._session_memory
        )
        self._confirmation_handler = confirmation_handler or ConfirmationHandler()
        self._app_matcher = FuzzyAppMatcher()
        self._intent_tracker = intent_tracker or IntentSessionTracker()
        self._workflow_store = workflow_store or workflow_memory
        self._workflow_policy = WorkflowPolicy()
        self._observer = observer or RuntimeObserver()
        self._validator = ExecutionValidator(memory, observer=self._observer)

    def plan(self, user_input: str) -> TaskPlan:
        goal = self._strip_planning_prefix(user_input)
        if not goal:
            return TaskPlan(
                intent="planner_message",
                fallback_response="I need a goal before I can build a plan.",
            )

        pending_feedback = self._pending_confirmation_feedback(goal)
        resolution = self._confirmation_handler.resolve(goal)
        if resolution is not None and resolution.matched:
            if pending_feedback is not None and not resolution.approved:
                self.record_strategy_outcome(
                    pending_feedback["goal"],
                    pending_feedback["plan"],
                    success=False,
                    progress={
                        "failure_reason": "user_rejected",
                        "failure_type": "intent_mismatch",
                        "checks": {"user_confirmation": False},
                        "user_feedback_weight": self._user_feedback_weight(pending_feedback["plan"]),
                        "parser_confidence": float(dict(pending_feedback["plan"].goal_state).get("parser_confidence", 1.0)),
                    },
                )
            if resolution.approved and resolution.plan is not None:
                resolution.plan.goal = resolution.goal or goal
                return resolution.plan
            return TaskPlan(
                intent="planner_message",
                goal=resolution.goal or goal,
                fallback_response=resolution.response,
                clarification_question=resolution.response,
            )

        if self._confirmation_handler.has_pending():
            logger.info("Replacing pending confirmation with a new request.")
            self._confirmation_handler.clear_pending()

        conflict_prompt = self._workflow_policy.detect_platform_conflict(goal)
        if conflict_prompt:
            return TaskPlan(
                intent="planner_message",
                goal=goal,
                fallback_response=conflict_prompt,
                clarification_question=conflict_prompt,
            )

        stability_anchor = self._stability_anchor(goal)
        parsed_preview = self._execution_planner.parse(goal)
        parser_feedback = self._parser_feedback_signal(goal, parsed_preview, stability_anchor=stability_anchor)
        context_signal = self._context_signal(parsed_preview)
        if parsed_preview.clarification_question:
            return TaskPlan(
                intent="planner_message",
                goal=goal,
                fallback_response=parsed_preview.clarification_question,
                clarification_question=parsed_preview.clarification_question,
            )
        if parser_feedback["clarification_question"]:
            question = str(parser_feedback["clarification_question"])
            return TaskPlan(
                intent="planner_message",
                goal=goal,
                fallback_response=question,
                clarification_question=question,
            )
        if context_signal["clarification_question"]:
            question = str(context_signal["clarification_question"])
            return TaskPlan(
                intent="planner_message",
                goal=goal,
                fallback_response=question,
                clarification_question=question,
            )

        workflow_plan = self._find_reusable_workflow(goal)
        if workflow_plan is not None:
            workflow_plan = self._prepare_plan(
                goal,
                workflow_plan,
                context_signal=context_signal,
                parser_feedback=parser_feedback,
                stability_anchor=stability_anchor,
            )
            workflow_plan.reused_from_memory = True
            return workflow_plan

        reused_plan = self._find_reusable_plan(goal)
        if reused_plan is not None:
            reused_plan = self._prepare_plan(
                goal,
                reused_plan,
                context_signal=context_signal,
                parser_feedback=parser_feedback,
                stability_anchor=stability_anchor,
            )
            reused_plan.reused_from_memory = True
            if self._requires_confirmation(reused_plan):
                prompt = self._confirmation_handler.queue(goal, reused_plan)
                return TaskPlan(
                    intent="planner_message",
                    goal=goal,
                    fallback_response=prompt,
                    clarification_question=prompt,
                    requires_approval=True,
                    overall_risk=reused_plan.overall_risk,
                    reused_from_memory=True,
                )
            return reused_plan

        plan = self._execution_planner.build_plan(goal)
        if not plan.steps:
            return plan

        plan = self._prepare_plan(
            goal,
            plan,
            context_signal=context_signal,
            parser_feedback=parser_feedback,
            stability_anchor=stability_anchor,
        )
        intent_assessment = self._intent_tracker.assess(goal, plan)
        if intent_assessment.clarification_question:
            return TaskPlan(
                intent="planner_message",
                goal=goal,
                fallback_response=intent_assessment.clarification_question,
                clarification_question=intent_assessment.clarification_question,
            )
        if self._requires_confirmation(plan):
            prompt = self._confirmation_handler.queue(goal, plan)
            return TaskPlan(
                intent="planner_message",
                goal=goal,
                fallback_response=prompt,
                clarification_question=prompt,
                requires_approval=True,
                overall_risk=plan.overall_risk,
                reused_from_memory=plan.reused_from_memory,
            )
        return plan

    def parse_command(self, user_input: str):
        return self._execution_planner.parse(user_input)

    def start_execution(self, plan: TaskPlan) -> None:
        self._confirmation_handler.activate(plan)

    def finish_execution(self) -> None:
        self._confirmation_handler.deactivate()

    def confirm_action(self, action_name: str, description: str) -> bool | None:
        return self._confirmation_handler.confirm_action(action_name, description)

    def session_snapshot(self):
        return self._session_memory.snapshot()

    def remember_fast_path_intent(
        self,
        user_input: str,
        *,
        action: str,
        target: str = "",
        params: dict[str, object] | None = None,
    ) -> None:
        self._execution_planner.remember_action(
            user_input=user_input,
            action=action,
            target=target,
            params=params,
        )

    def verify_step(self, step: StepState, result: ActionResult) -> tuple[bool, str]:
        verified, message, metadata = self._validator.verify_step(step, result)
        result.data.update(metadata)
        return verified, message

    def assess_goal_progress(
        self,
        plan: TaskPlan,
        *,
        task,
        workflow_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        return self._validator.goal_progress(plan, task=task, workflow_snapshot=workflow_snapshot)

    def record_strategy_outcome(
        self,
        goal: str,
        plan: TaskPlan,
        *,
        success: bool,
        progress: dict[str, Any] | None = None,
    ) -> None:
        if not plan.steps:
            return
        strategy_id = self._strategy_id_for(plan)
        failure_reason = self._classify_strategy_failure(plan, success=success, progress=progress)
        failure_type = self._classify_failure_type(plan, success=success, progress=progress)
        payload = {
            "strategy_id": strategy_id,
            "goal": goal,
            "normalized_goal": self._normalize_goal(goal),
            "success": bool(success),
            "failure_reason": failure_reason,
            "failure_type": failure_type,
            "goal_level": str(dict(plan.goal_state).get("goal_level", "primary")),
            "execution_cost": float((progress or {}).get("execution_cost", self._estimate_plan_cost(plan))),
            "parser_confidence": float((progress or {}).get("parser_confidence", dict(plan.goal_state).get("parser_confidence", 1.0))),
            "user_feedback_weight": float((progress or {}).get("user_feedback_weight", 1.0)),
            "stability_mode": str((progress or {}).get("stability_mode", dict(plan.goal_state).get("stability_mode", "adaptive"))),
            "goal_state": dict(plan.goal_state),
            "failed_checks": sorted(
                key
                for key, value in dict((progress or {}).get("checks", {})).items()
                if not value
            ),
        }
        self._memory.remember(
            namespace=_STRATEGY_MEMORY_NAMESPACE,
            content=json.dumps(payload, ensure_ascii=True),
            category=_STRATEGY_MEMORY_CATEGORY,
        )

    def build_goal_failure_response(
        self,
        goal: str,
        plan: TaskPlan,
        progress: dict[str, Any],
        default_message: str,
    ) -> str:
        recovery_decision = self.plan_goal_recovery(goal, plan, progress)
        if recovery_decision:
            mode = str(recovery_decision.get("mode", "")).strip().lower()
            if mode == "confirm":
                return f"{default_message} {recovery_decision['prompt']}"
            if mode in {"limit_reached", "blocked"}:
                return f"{default_message} {recovery_decision['message']}"
        return default_message

    def plan_goal_recovery(
        self,
        goal: str,
        plan: TaskPlan,
        progress: dict[str, Any],
    ) -> dict[str, Any] | None:
        if int(dict(plan.goal_state).get("retry_count", 0) or 0) >= int(
            dict(plan.goal_state).get("max_retry_count", _MAX_RECOVERY_ATTEMPTS) or _MAX_RECOVERY_ATTEMPTS
        ):
            return {
                "mode": "limit_reached",
                "message": (
                    "I stopped retrying because the recovery limit was reached. "
                    "The primary goal still was not confirmed."
                ),
            }

        candidates, blocked = self._available_recovery_strategies(goal, plan, progress)
        if not candidates:
            if blocked:
                return {
                    "mode": "blocked",
                    "message": self._blocked_recovery_message(plan, blocked),
                }
            return None

        chosen = self._select_recovery_strategy(goal, plan, candidates)
        retry_plan = chosen["plan"]
        if not isinstance(retry_plan, TaskPlan):
            return None

        retry_plan.goal = goal
        retry_plan.goal_state.setdefault("strategy_id", str(chosen["strategy_id"]))
        retry_plan.goal_state["retry_count"] = int(dict(plan.goal_state).get("retry_count", 0) or 0) + 1
        retry_plan.goal_state["max_retry_count"] = int(
            dict(plan.goal_state).get("max_retry_count", self._dynamic_retry_limit(plan)) or self._dynamic_retry_limit(plan)
        )
        retry_plan.goal_state.setdefault("requested_primary_goal", dict(plan.goal_state).get("requested_primary_goal") or dict(plan.goal_state))

        confidence = float(chosen.get("score", 0.0))
        if self._can_auto_execute_recovery(plan, retry_plan, chosen_score=confidence):
            return {
                "mode": "auto_execute",
                "plan": retry_plan,
                "strategy_id": str(chosen["strategy_id"]),
                "score": confidence,
                "message": str(
                    chosen.get(
                        "auto_message",
                        "The primary goal was not confirmed. I'm attempting a safe recovery automatically.",
                    )
                ),
            }

        prompt = str(chosen["prompt"])
        return {
            "mode": "confirm",
            "plan": retry_plan,
            "strategy_id": str(chosen["strategy_id"]),
            "score": confidence,
            "prompt": self._confirmation_handler.queue(goal, retry_plan, prompt=prompt),
        }

    def remember_successful_plan(self, goal: str, plan: TaskPlan) -> None:
        if not plan.steps:
            return
        self._intent_tracker.remember(goal, plan)
        payload = {
            "goal": goal,
            "normalized_goal": self._normalize_goal(goal),
            "tokens": sorted(self._tokenize(goal)),
            "context": build_command_context(goal, plan.steps),
            "intent": plan.intent,
            "goal_state": dict(plan.goal_state),
            "overall_risk": plan.overall_risk.value,
            "steps": [self._step_to_dict(step) for step in plan.steps],
        }
        self._memory.remember(
            namespace=_PLAN_MEMORY_NAMESPACE,
            content=json.dumps(payload, ensure_ascii=True),
            category=_PLAN_MEMORY_CATEGORY,
        )
        self._execution_planner.remember_success(goal, plan)

    def remember_session_plan(self, goal: str, plan: TaskPlan) -> None:
        if not plan.steps:
            return
        self._intent_tracker.remember(goal, plan)
        self._execution_planner.remember_success(goal, plan)

    def remember_successful_workflow(self, goal: str, workflow_steps: list[dict[str, Any]]) -> None:
        if not workflow_steps:
            return
        self._workflow_store.remember_successful_workflow(goal, workflow_steps)

    def remember_failed_workflow(self, goal: str, workflow_steps: list[dict[str, Any]]) -> None:
        if not workflow_steps:
            return
        self._workflow_store.remember_failed_workflow(goal, workflow_steps)

    def check_goal_completion(
        self,
        plan: TaskPlan,
        *,
        task,
        workflow_snapshot: dict[str, Any],
    ) -> tuple[bool, str]:
        return self._validator.goal_achieved(plan, task=task, workflow_snapshot=workflow_snapshot)

    def build_success_response(self, goal: str, plan: TaskPlan, messages: list[str]) -> str:
        del goal
        summary = self._success_summary(plan, messages)
        if plan.reused_from_memory:
            return f"{summary}\nplan_source: memory"
        return summary

    def build_failure_report(
        self,
        goal: str,
        step: StepState,
        result: ActionResult,
        *,
        plan: TaskPlan | None = None,
        fallback_attempted: bool,
        fallback_succeeded: bool,
    ) -> str:
        recovery = self._queue_recovery_follow_up(goal, step, result, plan)
        if recovery is not None:
            return recovery

        target = step.target or "that step"
        if result.status == "not_found":
            if step.action == "open_app":
                suggestion = self._app_matcher.suggest(step.target)
                if suggestion:
                    return f"I couldn't find {target}. Did you mean {format_app_name(suggestion)}?"
            return f"I couldn't find {target}."
        if result.error == "confirmation_required":
            return "I still need your confirmation before I can do that."
        if result.error == "cancelled":
            return "Stopped. I cancelled the remaining execution safely."

        details = self._flatten_messages([result.message or result.error or "unknown failure"])
        if fallback_attempted and not fallback_succeeded and step.fallback_action:
            return f"I couldn't complete {target}, and the fallback also failed. {details}"
        return f"I couldn't complete {target}. {details}".strip()

    def _requires_confirmation(self, plan: TaskPlan) -> bool:
        return any(
            step.action in {"delete_file", "overwrite_file", "install_app", "system_action"}
            or step.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}
            for step in plan.steps
        )

    def _find_reusable_plan(self, goal: str) -> TaskPlan | None:
        normalized_goal = self._normalize_goal(goal)
        goal_tokens = self._tokenize(goal)
        request_context = build_command_context(goal)
        best_payload: dict[str, Any] | None = None
        best_score = 0.0

        for record in self._memory.recall(query="", namespace=_PLAN_MEMORY_NAMESPACE, limit=50):
            try:
                payload = json.loads(record["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue

            candidate_goal = str(payload.get("normalized_goal", "")).strip()
            candidate_tokens = set(payload.get("tokens", []))
            steps = payload.get("steps", [])
            if not candidate_goal or not steps:
                continue
            if not contexts_match(payload.get("context"), request_context):
                continue

            score = 0.0
            if candidate_goal == normalized_goal:
                score = 1.0
            elif normalized_goal in candidate_goal or candidate_goal in normalized_goal:
                score = 0.85
            elif goal_tokens and candidate_tokens:
                union = goal_tokens | candidate_tokens
                if union:
                    score = len(goal_tokens & candidate_tokens) / len(union)

            if score >= 0.6 and score > best_score:
                best_score = score
                best_payload = payload

        if best_payload is None:
            return None
        return self._plan_from_payload(best_payload, requested_goal=goal)

    def _find_reusable_workflow(self, goal: str) -> TaskPlan | None:
        match = self._workflow_store.find_similar_workflow(goal)
        if match is None:
            return None

        steps: list[StepDefinition] = []
        for index, raw_step in enumerate(match.get("steps", []), start=1):
            params = dict(raw_step.get("params", {}))
            depends_on = tuple(int(item) for item in raw_step.get("depends_on", []))
            step = StepDefinition(
                action=str(raw_step.get("action", "")).strip(),
                step_id=int(raw_step.get("step_id", index)),
                target=str(raw_step.get("target", "")).strip(),
                params=params,
                depends_on=depends_on,
                description=self._workflow_step_description(raw_step),
                verification=self._workflow_step_verification(raw_step),
                risk_level=self._parse_risk_level(raw_step.get("risk_level")),
            )
            steps.append(step)

        if not steps:
            return None

        return TaskPlan(
            intent=str(steps[0].action if len(steps) == 1 else "workflow_memory"),
            goal=goal,
            steps=steps,
            overall_risk=max((step.risk_level for step in steps), key=self._risk_order, default=RiskLevel.SAFE),
            reused_from_memory=True,
        )

    def _plan_from_payload(self, payload: dict[str, Any], *, requested_goal: str) -> TaskPlan:
        steps: list[StepDefinition] = []
        for index, raw_step in enumerate(payload.get("steps", []), start=1):
            steps.append(
                StepDefinition(
                    action=str(raw_step.get("action", "")).strip(),
                    step_id=int(raw_step.get("step_id", index)),
                    target=str(raw_step.get("target", "")).strip(),
                    params=dict(raw_step.get("params", {})),
                    depends_on=tuple(int(item) for item in raw_step.get("depends_on", [])),
                    param_bindings=dict(raw_step.get("param_bindings", {})),
                    description=str(raw_step.get("description", "")).strip(),
                    verification=str(raw_step.get("verification", "")).strip(),
                    fallback_action=str(raw_step.get("fallback_action", "")).strip(),
                    fallback_target=str(raw_step.get("fallback_target", "")).strip(),
                    fallback_params=dict(raw_step.get("fallback_params", {})),
                    fallback_description=str(raw_step.get("fallback_description", "")).strip(),
                    fallback_verification=str(raw_step.get("fallback_verification", "")).strip(),
                    max_retries=max(0, int(raw_step.get("max_retries", 0))),
                    risk_level=self._parse_risk_level(raw_step.get("risk_level")),
                    expected_window=str(raw_step.get("expected_window", "")).strip(),
                )
            )
        return TaskPlan(
            intent=str(payload.get("intent", "reused_plan")),
            goal=requested_goal,
            steps=steps,
            goal_state=dict(payload.get("goal_state", {})),
            overall_risk=self._parse_risk_level(payload.get("overall_risk")),
        )

    @staticmethod
    def _step_to_dict(step: StepDefinition) -> dict[str, Any]:
        return {
            "step_id": step.step_id,
            "action": step.action,
            "target": step.target,
            "params": dict(step.params),
            "depends_on": list(step.depends_on),
            "param_bindings": dict(step.param_bindings),
            "description": step.description,
            "verification": step.verification,
            "fallback_action": step.fallback_action,
            "fallback_target": step.fallback_target,
            "fallback_params": dict(step.fallback_params),
            "fallback_description": step.fallback_description,
            "fallback_verification": step.fallback_verification,
            "max_retries": step.max_retries,
            "risk_level": step.risk_level.value if hasattr(step.risk_level, "value") else str(step.risk_level),
            "expected_window": step.expected_window,
        }

    def _prepare_plan(
        self,
        goal: str,
        plan: TaskPlan,
        *,
        context_signal: dict[str, Any] | None = None,
        parser_feedback: dict[str, Any] | None = None,
        stability_anchor: dict[str, Any] | None = None,
    ) -> TaskPlan:
        enriched = self._workflow_policy.enrich_plan(
            goal,
            plan,
            active_app=self._session_memory.snapshot().last_app,
        )
        enriched.goal = goal
        enriched.goal_state = self._validator.define_goal_state(enriched)
        enriched.goal_state.setdefault("strategy_id", self._strategy_id_for(enriched))
        enriched.goal_state.setdefault("retry_count", 0)
        enriched.goal_state.setdefault("max_retry_count", self._dynamic_retry_limit(enriched))
        enriched.goal_state.setdefault("goal_level", "primary")
        enriched.goal_state.setdefault("requested_primary_goal", dict(enriched.goal_state))
        enriched.goal_state.setdefault("acceptable_fallback_goals", self._acceptable_fallback_goals(enriched))
        enriched.goal_state.setdefault("intent_category", self._intent_category_for_plan(enriched))
        if parser_feedback:
            enriched.goal_state["parser_confidence"] = float(parser_feedback.get("confidence", 1.0))
            enriched.goal_state["parser_feedback_adjustment"] = float(parser_feedback.get("adjustment", 0.0))
            if parser_feedback.get("mismatch_count"):
                enriched.goal_state["parser_mismatch_count"] = int(parser_feedback["mismatch_count"])
        if stability_anchor:
            enriched.goal_state["stability_mode"] = str(stability_anchor.get("mode", "adaptive"))
            enriched.goal_state["stability_score"] = float(stability_anchor.get("score", 0.0))
        if context_signal:
            enriched.goal_state["context_confidence"] = float(context_signal.get("confidence", 1.0))
            enriched.goal_state["uses_context_memory"] = bool(context_signal.get("uses_context_memory"))
            if context_signal.get("age_seconds") is not None:
                enriched.goal_state["context_age_seconds"] = float(context_signal["age_seconds"])
            if context_signal.get("invalidated"):
                enriched.goal_state["context_invalidated"] = True
        return enriched

    def _context_signal(self, parsed_preview) -> dict[str, Any]:
        intents = list(getattr(parsed_preview, "intents", []) or [])
        uses_context_memory = any(str(getattr(intent, "route", "")).strip() == "command_memory" for intent in intents)
        base_confidence = float(getattr(parsed_preview, "confidence", 1.0) or 0.0)
        snapshot = self._session_memory.snapshot()
        signal: dict[str, Any] = {
            "uses_context_memory": uses_context_memory,
            "confidence": base_confidence,
            "age_seconds": None,
            "clarification_question": None,
            "invalidated": False,
        }
        if not uses_context_memory:
            return signal

        current_category = self._intent_category_for_intents(intents)
        previous_category = self._intent_category_for_action(snapshot.last_command)
        if current_category and previous_category and current_category != previous_category:
            signal["invalidated"] = True
            signal["confidence"] = base_confidence * 0.25
            signal["clarification_question"] = (
                "The previous context no longer matches this kind of request. Please restate the target so I keep your intent accurate."
            )
            return signal

        matched_ages: list[float] = []
        for intent in intents:
            if str(getattr(intent, "route", "")).strip() != "command_memory":
                continue
            age_seconds = self._matching_context_age(intent, snapshot)
            if age_seconds is not None:
                matched_ages.append(age_seconds)

        if matched_ages:
            max_age = max(matched_ages)
            signal["age_seconds"] = max_age
            signal["confidence"] = base_confidence * self._context_decay_multiplier(max_age)
        else:
            signal["confidence"] = base_confidence * 0.5

        if float(signal["confidence"]) < _MIN_CONTEXT_CONFIDENCE:
            signal["clarification_question"] = (
            "The context for that command may be stale. Please restate the target so I don't guess wrong."
            )
        return signal

    def _parser_feedback_signal(
        self,
        goal: str,
        parsed_preview,
        *,
        stability_anchor: dict[str, Any],
    ) -> dict[str, Any]:
        base_confidence = float(getattr(parsed_preview, "confidence", 1.0) or 0.0)
        normalized_goal = self._normalize_goal(goal)
        mismatch_penalty = 0.0
        mismatch_count = 0
        success_credit = 0.0
        for record in self._memory.recall(query="", namespace=_STRATEGY_MEMORY_NAMESPACE, limit=60):
            try:
                payload = json.loads(record["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if str(payload.get("normalized_goal", "")).strip() != normalized_goal:
                continue
            age_seconds = self._record_age_seconds(record)
            if age_seconds is None or age_seconds > _PARSER_MISMATCH_WINDOW_SECONDS:
                continue
            decay = max(0.0, 1.0 - (age_seconds / _PARSER_MISMATCH_WINDOW_SECONDS))
            if payload.get("success"):
                success_credit += 0.03 * decay
                continue
            if str(payload.get("failure_type", "")).strip().lower() == "intent_mismatch":
                if float(payload.get("parser_confidence", 1.0)) < 0.6:
                    continue  # don't let low-confidence failures penalise future intents
                mismatch_penalty += 0.05 * decay  # halved — one failure = 0.05 penalty max
                mismatch_count += 1

        if str(stability_anchor.get("mode", "adaptive")).strip().lower() == "baseline":
            mismatch_penalty += 0.06

        adjusted = max(0.0, min(1.0, base_confidence - mismatch_penalty + success_credit))
        clarification_question = None
        if adjusted < _PARSER_CONFIDENCE_FLOOR:
            clarification_question = "I may be misreading that request based on recent corrections. Please rephrase it more explicitly."
        return {
            "confidence": adjusted,
            "adjustment": adjusted - base_confidence,
            "mismatch_count": mismatch_count,
            "clarification_question": clarification_question,
        }

    def _pending_confirmation_feedback(self, user_input: str) -> dict[str, Any] | None:
        pending = getattr(self._confirmation_handler, "_pending", None)
        if pending is None:
            return None
        normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", user_input.strip().lower()).split())
        if normalized not in _NEGATIVE_REPLIES:
            return None
        if not isinstance(getattr(pending, "plan", None), TaskPlan):
            return None
        return {
            "goal": str(getattr(pending, "goal", "")).strip(),
            "plan": pending.plan,
        }

    @staticmethod
    def _matching_context_age(intent, snapshot) -> float | None:
        target = str(getattr(intent, "target", "")).strip().lower()
        action = str(getattr(intent, "action", "")).strip().lower()
        params = dict(getattr(intent, "params", {}) or {})
        for record in getattr(snapshot, "recent_contexts", ()):
            record_action = str(getattr(record, "action", "")).strip().lower()
            record_target = str(getattr(record, "target", "")).strip().lower()
            record_params = dict(getattr(record, "params", {}) or {})
            record_app = str(getattr(record, "primary_app", "")).strip().lower()
            if action == "play_youtube":
                query = str(params.get("query") or target).strip().lower()
                candidate = str(record_params.get("query") or record_target).strip().lower()
                if record_action == "search_youtube" and query and query == candidate:
                    return float(getattr(record, "age_seconds", 0.0))
            if target and target in {record_target, record_app}:
                return float(getattr(record, "age_seconds", 0.0))
            if action and action == record_action:
                return float(getattr(record, "age_seconds", 0.0))
        return None

    @staticmethod
    def _context_decay_multiplier(age_seconds: float) -> float:
        if age_seconds <= _CONTEXT_STALE_AFTER_SECONDS:
            return 1.0
        normalized_age = min(age_seconds, 180.0)
        decay = 1.0 - ((normalized_age - _CONTEXT_STALE_AFTER_SECONDS) / max(1.0, 180.0 - _CONTEXT_STALE_AFTER_SECONDS))
        return max(0.35, min(1.0, 0.35 + (0.65 * decay)))

    @staticmethod
    def _acceptable_fallback_goals(plan: TaskPlan) -> list[dict[str, Any]]:
        if not plan.steps:
            return []
        final_action = str(plan.steps[-1].action).strip().lower()
        if final_action != "play_youtube":
            return []
        query = str(plan.steps[-1].params.get("query") or plan.steps[-1].target).strip()
        if not query:
            return []
        return [
            {
                "state": "results_loaded",
                "platform": "youtube",
                "active_app": "youtube",
                "query": query,
                "url_contains": "youtube.com/results",
            }
        ]

    @staticmethod
    def _workflow_step_description(step: dict[str, Any]) -> str:
        action = str(step.get("action", "")).strip()
        target = str(step.get("target", "")).strip()
        return {
            "open_app": f"Open the {target} application.",
            "open_url": f"Open {target} in the browser.",
            "search_web": f"Search the web for {target}.",
            "search_youtube": f"Search YouTube for {target}.",
        }.get(action, f"Execute {action} for {target}.")

    @staticmethod
    def _workflow_step_verification(step: dict[str, Any]) -> str:
        action = str(step.get("action", "")).strip()
        return {
            "open_url": "confirm the URL was prepared",
            "search_web": "confirm the search URL was prepared",
            "search_youtube": "confirm the YouTube search URL was prepared",
            "open_app": "confirm the application launched",
        }.get(action, "action_result")

    @staticmethod
    def _strip_planning_prefix(text: str) -> str:
        stripped = text.strip()
        lowered = stripped.lower()
        for prefix in ("plan ", "goal "):
            if lowered.startswith(prefix):
                return stripped[len(prefix):].strip()
        return stripped

    @staticmethod
    def _normalize_goal(goal: str) -> str:
        return re.sub(r"\s+", " ", goal.strip().lower())

    @staticmethod
    def _tokenize(goal: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", goal.lower()))

    @staticmethod
    def _parse_risk_level(raw_value: object) -> RiskLevel:
        value = str(raw_value or RiskLevel.SAFE.value).strip().lower()
        if value in {level.value for level in RiskLevel}:
            return RiskLevel(value)
        return RiskLevel.SAFE

    @staticmethod
    def _run_verification_command(verification: str) -> tuple[bool, str]:
        match = re.fullmatch(r"check\s+([a-zA-Z0-9_.-]+)\s+--version", verification.strip(), re.IGNORECASE)
        if not match:
            return True, ""

        command = [match.group(1), "--version"]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except OSError as exc:
            return False, str(exc)

        output = (completed.stdout or completed.stderr or "").strip()
        return completed.returncode == 0, output

    @staticmethod
    def _flatten_messages(messages: list[str]) -> str:
        parts: list[str] = []
        for message in messages:
            for line in message.splitlines():
                cleaned = line.strip()
                if cleaned:
                    parts.append(cleaned)
        return " ".join(parts)

    def _queue_recovery_follow_up(
        self,
        goal: str,
        failed_step: StepState,
        result: ActionResult,
        plan: TaskPlan | None,
    ) -> str | None:
        if (
            plan is None
            or failed_step.action != "open_app"
            or result.status != "not_found"
            or failed_step.target.strip().lower() not in {"chrome", "google chrome", "comet", "edge", "microsoft edge", "firefox"}
        ):
            return None

        remaining_steps = [
            step
            for step in plan.steps
            if step.step_id > failed_step.plan_step_id
            and failed_step.plan_step_id in step.depends_on
            and step.action in {"search_web", "open_url"}
        ]
        if not remaining_steps:
            return None

        rewritten_steps: list[StepDefinition] = []
        for step in remaining_steps:
            params = dict(step.params)
            params.pop("browser_app", None)
            bindings = dict(step.param_bindings)
            bindings.pop("browser_app", None)
            rewritten_steps.append(
                replace(
                    step,
                    depends_on=tuple(dep for dep in step.depends_on if dep != failed_step.plan_step_id),
                    param_bindings=bindings,
                    params=params,
                    description=self._strip_browser_reference(step.description, failed_step.target),
                )
            )

        if not rewritten_steps:
            return None

        follow_up_plan = TaskPlan(
            intent="recovery_follow_up",
            goal=goal,
            steps=rewritten_steps,
            overall_risk=plan.overall_risk,
        )
        prompt = f"{failed_step.target.title()} isn't available. Should I use your default browser instead?"
        return self._confirmation_handler.queue(goal, follow_up_plan, prompt=prompt)

    def _available_recovery_strategies(
        self,
        goal: str,
        plan: TaskPlan,
        progress: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        strategies = self._candidate_recovery_strategies(goal, plan, progress)
        available: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for strategy in strategies:
            strategy_id = str(strategy.get("strategy_id", "")).strip()
            block_state = self._strategy_block_state(strategy_id)
            if block_state["blocked"]:
                blocked.append(block_state)
                continue
            available.append(strategy)
        return available, blocked

    def _candidate_recovery_strategies(
        self,
        goal: str,
        plan: TaskPlan,
        progress: dict[str, Any],
    ) -> list[dict[str, Any]]:
        del goal
        goal_state = dict(progress.get("goal_state") or plan.goal_state)
        state = str(goal_state.get("state", "")).strip().lower()
        action = str(goal_state.get("action", "")).strip().lower()
        query = str(goal_state.get("query", "")).strip()
        failed_checks = {
            key
            for key, value in dict(progress.get("checks", {})).items()
            if not value
        }

        if state != "video_playing" and action != "play_youtube":
            return []
        if not query:
            return []

        strategies: list[dict[str, Any]] = []
        if failed_checks & {"video_playing", "url_contains", "final_step_verified"}:
            search_step = StepDefinition(
                action="search_youtube",
                step_id=2,
                target=query,
                params={"query": query},
                depends_on=(1,),
                description=f"Search YouTube for {query}.",
                verification="confirm the YouTube search URL was prepared",
            )
            retry_plan = TaskPlan(
                intent="goal_recovery",
                goal=plan.goal,
                steps=[
                    StepDefinition(
                        action="open_url",
                        step_id=1,
                        target="youtube",
                        params={"url": "https://www.youtube.com"},
                        description="Open youtube in the browser.",
                        verification="confirm the URL for youtube was prepared",
                    ),
                    search_step,
                ],
                goal_state={
                    "action": "search_youtube",
                    "platform": "youtube",
                    "active_app": "youtube",
                    "state": "results_loaded",
                    "query": query,
                    "url_contains": "youtube.com/results",
                    "requires_verified_final_step": True,
                    "strategy_id": "youtube_results_recovery",
                    "recovery_for": self._strategy_id_for(plan),
                    "goal_level": "acceptable_fallback",
                    "requested_primary_goal": dict(plan.goal_state).get("requested_primary_goal") or dict(plan.goal_state),
                    "acceptable_fallback_goals": [
                        {
                            "state": "results_loaded",
                            "platform": "youtube",
                            "active_app": "youtube",
                            "query": query,
                            "url_contains": "youtube.com/results",
                        }
                    ],
                },
                overall_risk=RiskLevel.SAFE,
            )
            strategies.append(
                {
                    "strategy_id": "youtube_results_recovery",
                    "priority": 0.65,
                    "plan": retry_plan,
                    "prompt": (
                        f"I couldn't verify that the video started playing. "
                        f"I can open YouTube results for '{query}' so you can choose a video. "
                        "Reply yes to run that recovery strategy."
                    ),
                    "auto_message": (
                        f"I couldn't confirm playback for '{query}', so I'm loading YouTube results as a safe fallback."
                    ),
                }
            )

        retry_play_plan = TaskPlan(
            intent="goal_recovery",
            goal=plan.goal,
            steps=[
                StepDefinition(
                    action="open_url",
                    step_id=1,
                    target="youtube",
                    params={"url": "https://www.youtube.com"},
                    description="Open youtube in the browser.",
                    verification="confirm the URL for youtube was prepared",
                ),
                StepDefinition(
                    action="play_youtube",
                    step_id=2,
                    target=query,
                    params={"query": query},
                    depends_on=(1,),
                    description=f"Play YouTube result for {query}.",
                    verification="confirm YouTube video playback started",
                    fallback_action="search_youtube",
                    fallback_target=query,
                    fallback_params={"query": query},
                    fallback_description=f"Search YouTube for {query}.",
                    fallback_verification="confirm the YouTube search URL was prepared",
                ),
            ],
            goal_state={
                "action": "play_youtube",
                "platform": "youtube",
                "active_app": "youtube",
                "state": "video_playing",
                "query": query,
                "url_contains": "youtube.com/watch",
                "requires_verified_final_step": True,
                "strategy_id": "youtube_retry_playback",
                "recovery_for": self._strategy_id_for(plan),
                "goal_level": "primary",
                "requested_primary_goal": dict(plan.goal_state).get("requested_primary_goal") or dict(plan.goal_state),
                "acceptable_fallback_goals": list(dict(plan.goal_state).get("acceptable_fallback_goals", [])),
            },
            overall_risk=RiskLevel.SAFE,
        )
        strategies.append(
            {
                "strategy_id": "youtube_retry_playback",
                "priority": 0.35,
                "plan": retry_play_plan,
                "prompt": (
                    f"I couldn't verify playback for '{query}'. "
                    "I can retry by reopening YouTube and requesting playback again. "
                    "Reply yes to try that strategy."
                ),
                "auto_message": (
                    f"I couldn't confirm playback for '{query}', so I'm retrying the primary playback strategy automatically."
                ),
            }
        )
        return strategies

    def _select_recovery_strategy(
        self,
        goal: str,
        plan: TaskPlan,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        stability_mode = str(dict(plan.goal_state).get("stability_mode", "adaptive")).strip().lower()
        if stability_mode == "baseline":
            chosen = max(
                candidates,
                key=lambda item: (
                    float(item.get("priority", 0.0)),
                    self._strategy_success_rate(str(item.get("strategy_id", ""))),
                ),
            )
            chosen["score"] = float(chosen.get("priority", 0.0))
            chosen["selection_mode"] = "baseline"
            return chosen

        ranked = sorted(
            candidates,
            key=lambda item: self._strategy_rank(item, goal=goal),
            reverse=True,
        )
        retry_count = int(dict(plan.goal_state).get("retry_count", 0) or 0)
        has_history = any(self._strategy_attempt_count(str(item.get("strategy_id", ""))) > 0 for item in ranked)
        if (
            stability_mode != "baseline"
            and has_history
            and len(ranked) > 1
            and self._should_explore(goal, retry_count=retry_count, ranked=ranked)
        ):
            exploratory = ranked[1]
            exploratory["score"] = self._strategy_rank(exploratory, goal=goal)
            exploratory["selection_mode"] = "explore"
            return exploratory

        chosen = ranked[0]
        chosen["score"] = self._strategy_rank(chosen, goal=goal)
        chosen["selection_mode"] = "exploit"
        return chosen

    def _strategy_rank(self, candidate: dict[str, Any], *, goal: str) -> float:
        strategy_score = self._strategy_success_rate(str(candidate["strategy_id"]))
        priority = float(candidate.get("priority", 0.0))
        execution_cost = self._strategy_execution_cost(
            str(candidate["strategy_id"]),
            plan=candidate.get("plan"),
        )
        temporal_penalty = self._strategy_temporal_penalty(str(candidate["strategy_id"]))
        return (strategy_score * 0.7) + (priority * 0.3) - (execution_cost * _STRATEGY_COST_WEIGHT) - temporal_penalty

    def _should_explore(self, goal: str, *, retry_count: int, ranked: list[dict[str, Any]]) -> bool:
        if len(ranked) < 2:
            return False
        time_window = int(time.time() // _EXPLORATION_TIME_WINDOW_SECONDS)
        repetition_count = self._similar_goal_repetition_count(goal)
        effective_rate = _STRATEGY_EXPLORATION_RATE / max(1.0, 1.0 + (0.45 * repetition_count))
        seed = "|".join(
            [
                goal.strip().lower(),
                str(time_window),
                str(retry_count + 1),
                ",".join(str(item.get("strategy_id", "")) for item in ranked),
            ]
        )
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        value = int.from_bytes(digest[:8], byteorder="big", signed=False) / float(2**64)
        return value < effective_rate

    def _strategy_success_rate(self, strategy_id: str) -> float:
        records = self._memory.recall(
            query=strategy_id,
            namespace=_STRATEGY_MEMORY_NAMESPACE,
            limit=100,
        )
        successes = 0.0
        effective_failures = 0.0
        for record in records:
            try:
                payload = json.loads(record["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if payload.get("strategy_id") != strategy_id:
                continue
            if payload.get("success"):
                successes += 1.0
                continue
            failure_reason = str(payload.get("failure_reason", "")).strip().lower()
            failure_type = str(payload.get("failure_type", "")).strip().lower()
            if failure_reason == "capability_limit":
                effective_failures += _CAPABILITY_FAILURE_WEIGHT
            elif failure_reason == "user_rejected":
                feedback_weight = float(payload.get("user_feedback_weight", 1.0) or 1.0)
                effective_failures += _USER_REJECTION_FAILURE_WEIGHT * feedback_weight
            elif failure_type == "intent_mismatch":
                effective_failures += _INTENT_MISMATCH_FAILURE_WEIGHT
            elif failure_reason not in {"cancelled", "user_cancelled"}:
                effective_failures += 1.0
        return (successes + 1.0) / (successes + effective_failures + 2.0)

    def _strategy_attempt_count(self, strategy_id: str) -> int:
        records = self._memory.recall(
            query=strategy_id,
            namespace=_STRATEGY_MEMORY_NAMESPACE,
            limit=100,
        )
        total = 0
        for record in records:
            try:
                payload = json.loads(record["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if payload.get("strategy_id") == strategy_id:
                total += 1
        return total

    def _strategy_execution_cost(
        self,
        strategy_id: str,
        *,
        plan: TaskPlan | None,
    ) -> float:
        records = self._memory.recall(
            query=strategy_id,
            namespace=_STRATEGY_MEMORY_NAMESPACE,
            limit=50,
        )
        costs: list[float] = []
        for record in records:
            try:
                payload = json.loads(record["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if payload.get("strategy_id") != strategy_id:
                continue
            raw_cost = payload.get("execution_cost")
            if raw_cost in {None, ""}:
                continue
            try:
                costs.append(float(raw_cost))
            except (TypeError, ValueError):
                continue
        if costs:
            mean_cost = sum(costs) / len(costs)
            variability = (max(costs) - min(costs)) if len(costs) > 1 else 0.0
            return mean_cost + (variability * 0.15)
        if isinstance(plan, TaskPlan):
            return self._estimate_plan_cost(plan)
        return 1.0

    @staticmethod
    def _can_auto_execute_recovery(
        parent_plan: TaskPlan,
        retry_plan: TaskPlan,
        *,
        chosen_score: float,
    ) -> bool:
        if retry_plan.overall_risk != RiskLevel.SAFE:
            return False
        if str(dict(parent_plan.goal_state).get("stability_mode", "adaptive")).strip().lower() == "baseline":
            return False
        primary_goal = dict(parent_plan.goal_state).get("requested_primary_goal") or dict(parent_plan.goal_state)
        if not GoalPlanner._goals_are_equivalent(primary_goal, retry_plan.goal_state):
            return False
        context_confidence = float(dict(parent_plan.goal_state).get("context_confidence", 1.0) or 0.0)
        return chosen_score >= _AUTO_RECOVERY_CONFIDENCE and context_confidence >= _MIN_CONTEXT_CONFIDENCE

    @staticmethod
    def _classify_strategy_failure(
        plan: TaskPlan,
        *,
        success: bool,
        progress: dict[str, Any] | None,
    ) -> str:
        if success:
            return "success"
        details = dict(progress or {})
        explicit_reason = str(details.get("failure_reason", "") or details.get("reason", "")).strip().lower()
        if explicit_reason:
            return explicit_reason
        failed_checks = {
            key
            for key, value in dict(details.get("checks", {})).items()
            if not value
        }
        if failed_checks and failed_checks <= {
            "final_step_verified",
            "active_app",
            "browser_app",
            "url_contains",
            "query_matches",
            "video_playing",
        }:
            return "capability_limit"
        if int(details.get("completed_steps", 0) or 0) >= int(details.get("total_steps", 0) or 0) and failed_checks:
            return "capability_limit"
        if str(dict(plan.goal_state).get("goal_level", "")).strip().lower() == "acceptable_fallback":
            return "fallback_incomplete"
        return "strategy_failure"

    @staticmethod
    def _classify_failure_type(
        plan: TaskPlan,
        *,
        success: bool,
        progress: dict[str, Any] | None,
    ) -> str:
        if success:
            return "success"
        details = dict(progress or {})
        explicit = str(details.get("failure_type", "")).strip().lower()
        if explicit:
            return explicit
        failed_checks = {
            key
            for key, value in dict(details.get("checks", {})).items()
            if not value
        }
        if "query_matches" in failed_checks:
            return "intent_mismatch"
        if failed_checks & {"active_app", "browser_app"} and str(details.get("failure_reason", "")).strip().lower() == "user_rejected":
            return "intent_mismatch"
        if GoalPlanner._classify_strategy_failure(plan, success=False, progress=details) == "capability_limit":
            return "capability_limit"
        if str(details.get("failure_reason", "")).strip().lower() == "user_rejected":
            return "intent_mismatch"
        return "strategy_failure"

    def _strategy_block_state(self, strategy_id: str) -> dict[str, Any]:
        if not strategy_id:
            return {"blocked": False, "strategy_id": strategy_id}
        records = self._memory.recall(
            query=strategy_id,
            namespace=_STRATEGY_MEMORY_NAMESPACE,
            limit=10,
        )
        capability_failures = 0
        for record in records:
            age_seconds = self._record_age_seconds(record)
            if age_seconds is not None and age_seconds > _STRATEGY_BLOCK_COOLDOWN_SECONDS:
                break
            try:
                payload = json.loads(record["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if payload.get("strategy_id") != strategy_id:
                continue
            if payload.get("success"):
                break
            failure_reason = str(payload.get("failure_reason", "")).strip().lower()
            failure_type = str(payload.get("failure_type", "")).strip().lower()
            if failure_reason == "capability_limit" or failure_type == "capability_limit":
                capability_failures += 1
                if capability_failures >= _CAPABILITY_BLOCK_THRESHOLD:
                    return {
                        "blocked": True,
                        "strategy_id": strategy_id,
                        "reason": "capability_limit",
                        "cooldown_seconds": max(0.0, _STRATEGY_BLOCK_COOLDOWN_SECONDS - float(age_seconds or 0.0)),
                    }
            else:
                break
        return {"blocked": False, "strategy_id": strategy_id}

    def _strategy_temporal_penalty(self, strategy_id: str) -> float:
        if not strategy_id:
            return 0.0
        records = self._memory.recall(
            query=strategy_id,
            namespace=_STRATEGY_MEMORY_NAMESPACE,
            limit=12,
        )
        penalty = 0.0
        for record in records:
            age_seconds = self._record_age_seconds(record)
            if age_seconds is None or age_seconds > _STRATEGY_PENALTY_COOLDOWN_SECONDS:
                continue
            try:
                payload = json.loads(record["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if payload.get("strategy_id") != strategy_id:
                continue
            decay = max(0.0, 1.0 - (age_seconds / _STRATEGY_PENALTY_COOLDOWN_SECONDS))
            if payload.get("success"):
                penalty = max(0.0, penalty - (0.08 * decay))
                continue
            failure_reason = str(payload.get("failure_reason", "")).strip().lower()
            failure_type = str(payload.get("failure_type", "")).strip().lower()
            if failure_reason == "user_rejected":
                feedback_weight = float(payload.get("user_feedback_weight", 1.0) or 1.0)
                penalty += 0.1 * feedback_weight * decay
            elif failure_reason == "capability_limit":
                penalty += 0.12 * decay
            elif failure_type == "intent_mismatch":
                penalty += 0.11 * decay
            else:
                penalty += 0.06 * decay
        return min(0.5, penalty)

    @staticmethod
    def _blocked_recovery_message(plan: TaskPlan, blocked: list[dict[str, Any]]) -> str:
        cooldown_seconds = max(float(item.get("cooldown_seconds", 0.0) or 0.0) for item in blocked) if blocked else 0.0
        requested = dict(plan.goal_state).get("requested_primary_goal") or dict(plan.goal_state)
        state = str(requested.get("state", "")).strip().lower()
        if state == "video_playing":
            return (
                "I stopped retrying playback because this environment keeps hitting the same capability limit. "
                f"I need a different execution path or explicit user choice. Cooldown remaining: about {int(cooldown_seconds)}s."
            )
        return (
            "I stopped retrying because the remaining recovery strategies are temporarily blocked by the current environment. "
            f"Cooldown remaining: about {int(cooldown_seconds)}s."
        )

    @staticmethod
    def _estimate_plan_cost(plan: TaskPlan) -> float:
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
        if not plan.steps:
            return 0.0
        return sum(action_weights.get(str(step.action).strip().lower(), 0.5) for step in plan.steps)

    def _similar_goal_repetition_count(self, goal: str) -> int:
        normalized_goal = self._normalize_goal(goal)
        count = 0
        for record in self._memory.recall(query="", namespace=_STRATEGY_MEMORY_NAMESPACE, limit=80):
            try:
                payload = json.loads(record["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if str(payload.get("normalized_goal", "")).strip() == normalized_goal:
                count += 1
        return count

    def _stability_anchor(self, goal: str) -> dict[str, Any]:
        del goal
        records = self._memory.recall(query="", namespace=_STRATEGY_MEMORY_NAMESPACE, limit=_STABILITY_WINDOW_RECORDS)
        if len(records) < _STABILITY_MIN_RECORDS:
            return {"mode": "adaptive", "score": 0.0}
        failures = 0
        intent_mismatches = 0
        for record in records:
            try:
                payload = json.loads(record["content"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if not payload.get("success"):
                failures += 1
            if str(payload.get("failure_type", "")).strip().lower() == "intent_mismatch":
                intent_mismatches += 1
        ratio = failures / max(1, len(records))
        score = ratio + (0.08 * intent_mismatches)
        if ratio >= _STABILITY_FAILURE_RATIO:
            return {"mode": "baseline", "score": score}
        return {"mode": "adaptive", "score": score}

    @staticmethod
    def _record_age_seconds(record: dict[str, Any]) -> float | None:
        raw_timestamp = str(record.get("timestamp", "")).strip()
        if not raw_timestamp:
            return None
        try:
            timestamp = datetime.fromisoformat(raw_timestamp)
        except ValueError:
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds())

    @staticmethod
    def _user_feedback_weight(plan: TaskPlan) -> float:
        confidence = float(dict(plan.goal_state).get("parser_confidence", dict(plan.goal_state).get("context_confidence", 1.0)) or 0.0)
        return max(0.55, min(1.5, 0.55 + confidence))

    @staticmethod
    def _goals_are_equivalent(primary_goal: dict[str, Any], candidate_goal: dict[str, Any]) -> bool:
        primary = dict(primary_goal or {})
        candidate = dict(candidate_goal or {})
        comparable_keys = ("action", "state", "platform", "active_app", "query", "url_contains")
        return all(
            str(primary.get(key, "")).strip().lower() == str(candidate.get(key, "")).strip().lower()
            for key in comparable_keys
            if str(primary.get(key, "")).strip() or str(candidate.get(key, "")).strip()
        )

    @staticmethod
    def _intent_category_for_action(action: str) -> str:
        normalized = str(action or "").strip().lower()
        if normalized in {"play_youtube", "search_youtube"}:
            return "media"
        if normalized in {"open_url", "search_web"}:
            return "web"
        if normalized in {"open_app", "focus_app", "close_app", "switch_window"}:
            return "app"
        if normalized in {"create_file", "overwrite_file", "read_file", "delete_file"}:
            return "file"
        if normalized in {"remember_fact", "recall_memory", "set_reminder"}:
            return "memory"
        return "system"

    @classmethod
    def _intent_category_for_intents(cls, intents: list[Any]) -> str:
        categories = {
            cls._intent_category_for_action(str(getattr(intent, "action", "")).strip())
            for intent in intents
            if str(getattr(intent, "action", "")).strip()
        }
        if not categories:
            return ""
        if "media" in categories:
            return "media"
        return sorted(categories)[0]

    @classmethod
    def _intent_category_for_plan(cls, plan: TaskPlan) -> str:
        if not plan.steps:
            return ""
        return cls._intent_category_for_action(str(plan.steps[-1].action).strip())

    @staticmethod
    def _dynamic_retry_limit(plan: TaskPlan) -> int:
        goal_state = dict(plan.goal_state)
        state = str(goal_state.get("state", "")).strip().lower()
        action = str(goal_state.get("action", "")).strip().lower()
        if action in {"delete_file", "overwrite_file", "install_app", "system_action"}:
            return 1
        if state == "video_playing" or action == "play_youtube":
            return 1
        if state in {"page_loaded", "results_loaded", "app_running"}:
            return 2
        return _MAX_RECOVERY_ATTEMPTS

    @staticmethod
    def _strategy_id_for(plan: TaskPlan) -> str:
        explicit = str(dict(plan.goal_state).get("strategy_id", "")).strip()
        if explicit:
            return explicit
        if not plan.steps:
            return "no_steps"
        final_step = plan.steps[-1]
        action = str(final_step.action).strip().lower()
        state = str(dict(plan.goal_state).get("state", "")).strip().lower()
        return f"default:{action}:{state or 'unmodeled'}"

    @staticmethod
    def _strip_browser_reference(description: str, browser_name: str) -> str:
        cleaned = re.sub(
            rf"\s+in\s+{re.escape(browser_name.strip())}\.?",
            ".",
            description,
            flags=re.IGNORECASE,
        ).strip()
        return cleaned or description

    def _success_summary(self, plan: TaskPlan, messages: list[str]) -> str:
        goal_level = str(dict(plan.goal_state).get("goal_level", "primary")).strip().lower()
        if goal_level == "acceptable_fallback":
            fallback_target = plan.steps[-1].target if plan.steps else "the fallback target"
            return f"Done. I couldn't complete the primary goal, but I loaded a safe fallback for {fallback_target}."
        if len(plan.steps) == 1:
            if plan.steps[0].action in {"recall_memory", "read_file", "get_clipboard"} and messages:
                return self._flatten_messages(messages)
            return f"Done. {self._past_tense(plan.steps[0])}"

        step_bits = [self._past_tense(step) for step in plan.steps[:3]]
        summary = "Done. " + " Then ".join(step_bits)
        if len(plan.steps) > 3:
            summary += f" (+{len(plan.steps) - 3} more steps)."
        return summary

    @staticmethod
    def _past_tense(step: StepDefinition) -> str:
        target = step.target or str(step.params.get("query") or step.params.get("url") or "").strip()
        browser_app = str(step.params.get("browser_app", "")).strip()
        if step.action == "open_app":
            return f"opened {target}."
        if step.action == "search_web":
            if browser_app:
                return f"searched {target} in {browser_app}."
            return f"searched {target}."
        if step.action == "search_youtube":
            return f"searched YouTube for {target}."
        if step.action == "open_url":
            if browser_app:
                return f"opened {target} in {browser_app}."
            return f"opened {target}."
        if step.action == "create_file":
            return f"created {target}."
        if step.action == "delete_file":
            return f"deleted {target}."
        if step.action == "remember_fact":
            return "saved that to memory."
        if step.action == "recall_memory":
            return "checked memory."
        return step.description.rstrip(".") + "."

    @staticmethod
    def _risk_order(risk: RiskLevel) -> int:
        return {
            RiskLevel.SAFE: 0,
            RiskLevel.LOW: 1,
            RiskLevel.MEDIUM: 2,
            RiskLevel.HIGH: 3,
            RiskLevel.CRITICAL: 4,
        }[risk]
