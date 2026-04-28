from __future__ import annotations

import re
from typing import Callable

from jarvis.core.intent_classifier import ClassifiedIntent, IntentClassifier
from jarvis.runtime.planner import ExecutionPlanner
from jarvis.runtime.execution_types import (
    ExecutionIntent,
    ExecutionState,
    ExecutionStep,
    OpenIntent,
    PlayIntent,
    SearchIntent,
    NoOpIntent,
    OrchestratorStepIntent,
)


class CommandSplitter:
    @staticmethod
    def split(text: str) -> list[str]:
        return [part.strip() for part in re.split(r"\s+and\s+|\s+then\s+|,\s+", text, flags=re.IGNORECASE) if part.strip()]


class CommandPrioritizer:
    @staticmethod
    def _is_play(classified: ClassifiedIntent | None) -> bool:
        return classified is not None and isinstance(classified.intent, PlayIntent) and bool(classified.intent.query)

    @staticmethod
    def _intent_priority(classified: ClassifiedIntent | None) -> int:
        if classified is None:
            return 4
        intent = classified.intent
        if isinstance(intent, OpenIntent):
            return 1
        if isinstance(intent, SearchIntent):
            return 2
        if isinstance(intent, PlayIntent):
            return 3
        return 4

    @staticmethod
    def _is_redundant_search(classified: ClassifiedIntent | None, play_queries: set[str]) -> bool:
        return (
            classified is not None
            and isinstance(classified.intent, SearchIntent)
            and classified.intent.query in play_queries
        )

    @classmethod
    def prioritize(cls, commands: list[str], classify_fn: Callable[[str], ClassifiedIntent | None]) -> list[str]:
        resolved = [(command, classify_fn(command)) for command in commands]
        if not any(cls._is_play(classified) for _, classified in resolved):
            return commands

        resolved.sort(key=lambda item: cls._intent_priority(item[1]))
        play_queries = {
            classified.intent.query
            for _, classified in resolved
            if cls._is_play(classified)
        }
        filtered = [
            command
            for command, classified in resolved
            if not cls._is_redundant_search(classified, play_queries)
        ]
        return filtered or commands


class IntentResolver:
    def __init__(
        self,
        classifier: IntentClassifier,
        contextual_resolver: Callable[[str, ExecutionState], ExecutionIntent | None],
        confidence_floor: float = 0.85,
    ) -> None:
        self._classifier = classifier
        self._contextual_resolver = contextual_resolver
        self._confidence_floor = confidence_floor

    def classify(self, text: str) -> ClassifiedIntent | None:
        classify_intent = getattr(self._classifier, "classify_intent", None)
        if callable(classify_intent):
            return classify_intent(text)
        intent, confidence = self._classifier.classify_with_confidence(text)
        if intent is None:
            return None
        return ClassifiedIntent(intent=intent, confidence=confidence)

    def resolve(self, text: str, state: ExecutionState) -> tuple[ExecutionIntent | None, str, float]:
        classified = self.classify(text)
        if classified is not None and classified.confidence >= self._confidence_floor:
            return classified.intent, "matched", classified.confidence
        fallback = self._contextual_resolver(text, state)
        if fallback is not None:
            return fallback, "fallback", 1.0
        return (
            (classified.intent if classified is not None else None),
            "matched",
            (classified.confidence if classified is not None else 0.0),
        )


class StepAssembler:
    def __init__(self, planner: ExecutionPlanner | None) -> None:
        self._planner = planner

    async def assemble_from_plan(self, text: str, state: ExecutionState) -> list[ExecutionStep] | None:
        if self._planner is None:
            return None
        steps = await self._planner.plan(text, state)
        return steps if steps else None

    def assemble_from_intent(
        self,
        intent: ExecutionIntent | None,
        source: str,
        dependencies: tuple[str, ...] = (),
        confidence: float = 1.0,
    ) -> ExecutionStep | None:
        if intent is None:
            return None
        step = ExecutionStep(intent=intent, source=source, dependencies=dependencies)
        step.metadata["confidence"] = confidence
        return step


class PlanValidator:
    @staticmethod
    def validate(steps: list[ExecutionStep], original_text: str) -> tuple[bool, str]:
        if not steps:
            return False, "Empty plan"
            
        # 1. Hallucination check: Vague single-step plans
        if len(steps) == 1:
            intent = steps[0].intent
            if isinstance(intent, NoOpIntent):
                return False, "Vague NoOpIntent generated"
            if isinstance(intent, OrchestratorStepIntent):
                # Check if it's just repeating the goal without an action
                action = getattr(intent.step_obj, "action", "").lower()
                if not action or action in {"process", "execute", "handle", "step"}:
                    return False, f"Vague planner action: {action}"

        # 2. Structural check: Circular dependencies
        ids = {s.id for s in steps}
        for s in steps:
            for dep in s.dependencies:
                if dep not in ids:
                    return False, f"Broken dependency: {dep}"
                if dep == s.id:
                    return False, "Self-referencing dependency"

        # 3. Content check: Is it relevant?
        if all(isinstance(s.intent, NoOpIntent) for s in steps):
            return False, "Plan contains only NoOpIntents"

        # 4. Semantic check: unwrap OrchestratorStepIntent steps (warning, not hard fail)
        warnings: list[str] = []
        for s in steps:
            if isinstance(s.intent, OrchestratorStepIntent):
                step_obj = s.intent.step_obj
                action = None
                try:
                    action = getattr(step_obj, "action", None) or getattr(step_obj, "type", None)
                except Exception:
                    pass
                if not action or (
                    isinstance(action, str)
                    and action.lower() in {"process", "execute", "handle", "step", "unknown"}
                ):
                    warnings.append(
                        f"Step {s.id[:8]}: vague/unreadable OrchestratorStepIntent action: {action!r}"
                    )
        if warnings:
            print(f"[VALIDATOR] Warnings: {'; '.join(warnings)}", flush=True)

        return True, "Valid"


class PlanScorer:
    """Scores a candidate plan on a 0.0–1.0 scale (higher = better).
    
    Signals used:
      - Step count  (fewer is better)
      - Failure history penalties (known bad intent hashes)
      - Confidence penalties (historically flaky intents)
      - Redundancy (duplicate intents in same plan)
    """
    BASE_SCORE = 1.0
    STEP_COUNT_WEIGHT = 0.05   # penalty per extra step beyond 1
    FAILURE_PENALTY = 0.20     # per step with known failure history
    CONFIDENCE_PENALTY_WEIGHT = 0.5  # scale accumulated confidence penalties
    REDUNDANCY_PENALTY = 0.15  # per duplicate intent in plan
    SCORE_THRESHOLD = 0.40     # reject plans below this

    def __init__(
        self,
        failure_history: dict[str, int] | None = None,
        confidence_penalties: dict[str, float] | None = None,
        strategy_scores: dict[str, float] | None = None,
    ) -> None:
        self._failure_history = failure_history or {}
        self._confidence_penalties = confidence_penalties or {}
        self._strategy_scores = strategy_scores or {}

    def _intent_hash(self, step: ExecutionStep) -> str:
        intent = step.intent
        action = getattr(intent, "type", intent.__class__.__name__)
        query = getattr(intent, "query", getattr(intent, "target", ""))
        strategy_id = step.metadata.get("strategy_id", "")
        return f"{action}:{query}:{strategy_id}"

    def score(self, steps: list[ExecutionStep], goal: str = "") -> float:
        if not steps:
            return 0.0

        score = self.BASE_SCORE

        # 1. Step count penalty (target = 1 step, penalise each extra)
        extra_steps = max(0, len(steps) - 1)
        score -= extra_steps * self.STEP_COUNT_WEIGHT

        seen_hashes: set[str] = set()
        for step in steps:
            h = self._intent_hash(step)

            # 2. Known failure history penalty
            failure_count = self._failure_history.get(h, 0)
            if failure_count > 0:
                score -= min(failure_count, 3) * (self.FAILURE_PENALTY / 3)

            # 3. Accumulated confidence penalty
            cp = self._confidence_penalties.get(h, 0.0)
            score -= cp * self.CONFIDENCE_PENALTY_WEIGHT

            # 4. Redundancy penalty (same intent appears twice in this plan)
            if h in seen_hashes:
                score -= self.REDUNDANCY_PENALTY
            seen_hashes.add(h)

            # 5. Strategy score bonus
            strategy_id = step.metadata.get("strategy_id", "")
            if strategy_id:
                score += self._strategy_scores.get(strategy_id, 0.0) * 0.1

        return max(0.0, min(1.0, score))

    def is_acceptable(self, steps: list[ExecutionStep], goal: str = "") -> tuple[bool, float]:
        s = self.score(steps, goal=goal)
        return s >= self.SCORE_THRESHOLD, s

    def score_all(self, candidates: list[list[ExecutionStep]], goal: str = "") -> list[dict[str, float | int]]:
        scored: list[dict[str, float | int]] = []
        for index, plan in enumerate(candidates, start=1):
            scored.append(
                {
                    "candidate_index": index,
                    "score": self.score(plan, goal=goal),
                    "step_count": len(plan),
                }
            )
        return scored

    def best_of(self, candidates: list[list[ExecutionStep]], goal: str = "") -> list[ExecutionStep] | None:
        """Pick the highest-scoring non-empty candidate plan."""
        best_plan: list[ExecutionStep] | None = None
        best_score = -1.0
        for plan in candidates:
            s = self.score(plan, goal=goal)
            if s > best_score:
                best_score = s
                best_plan = plan
        return best_plan


class CommandProcessor:
    def __init__(
        self,
        *,
        classifier: IntentClassifier,
        planner: ExecutionPlanner | None,
        contextual_resolver: Callable[[str, ExecutionState], ExecutionIntent | None],
        classifier_confidence_floor: float = 0.85,
        plan_scorer: PlanScorer | None = None,
    ) -> None:
        self._splitter = CommandSplitter()
        self._resolver = IntentResolver(
            classifier=classifier,
            contextual_resolver=contextual_resolver,
            confidence_floor=classifier_confidence_floor,
        )
        self._prioritizer = CommandPrioritizer()
        self._assembler = StepAssembler(planner=planner)
        self._scorer = plan_scorer or PlanScorer()

    async def build_steps(self, text: str, state: ExecutionState) -> tuple[list[ExecutionStep], dict[str, int]]:
        import dataclasses
        steps: list[ExecutionStep] = []
        metrics = {"matched": 0, "fallback": 0, "planner_calls": 0, "planner_steps": 0}
        preclassified_intent = state.context.get("preclassified_intent")
        tier_hint = state.context.get("tier_hint") or {}
        preclassified_confidence = float(tier_hint.get("hint_confidence", 1.0) or 1.0)
        
        # 1. True Planner Dominance: Give the planner the entire unaltered text first
        planned_steps = await self._assembler.assemble_from_plan(text, state)
        
        # Plan validation (Decision Intelligence — gate 1: structural)
        if planned_steps:
            is_valid, reason = PlanValidator.validate(planned_steps, text)
            if not is_valid:
                print(f"[PROCESSOR] Rejecting planner output (validator): {reason}", flush=True)
            else:
                # Plan scoring (Decision Intelligence — gate 2: quality)
                is_acceptable, plan_score = self._scorer.is_acceptable(planned_steps, goal=text)
                if not is_acceptable:
                    print(f"[PROCESSOR] Rejecting planner output (scorer): score={plan_score:.2f} < {PlanScorer.SCORE_THRESHOLD}", flush=True)
                else:
                    print(f"[PROCESSOR] Accepting planner plan: score={plan_score:.2f}, steps={len(planned_steps)}", flush=True)
                    metrics["planner_calls"] += 1
                    metrics["planner_steps"] += len(planned_steps)
                    # Ensure a fully connected DAG if planner missed dependencies
                    for i in range(1, len(planned_steps)):
                        if not planned_steps[i].dependencies:
                            planned_steps[i] = dataclasses.replace(planned_steps[i], dependencies=(planned_steps[i-1].id,))
                    steps.extend(planned_steps)
                    return steps, metrics

        if preclassified_intent is not None:
            hinted_step = self._assembler.assemble_from_intent(
                preclassified_intent,
                "matched",
                confidence=preclassified_confidence,
            )
            if hinted_step is not None:
                metrics["matched"] += 1
                steps.append(hinted_step)
                return steps, metrics

        # 2. Fallback to Classifier Pipeline (split and resolve)
        commands = self._splitter.split(text)
        if len(commands) > 1:
            commands = self._prioritizer.prioritize(commands, self._resolver.classify)

        previous_step_id = None
        for command_text in commands:
            intent, source, confidence = self._resolver.resolve(command_text, state)
            dependencies = (previous_step_id,) if previous_step_id else ()
            step = self._assembler.assemble_from_intent(
                intent, source, dependencies=dependencies, confidence=confidence,
            )
            if step is not None:
                metrics[source] += 1
                steps.append(step)
                previous_step_id = step.id
        return steps, metrics
