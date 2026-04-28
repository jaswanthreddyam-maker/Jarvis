from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Protocol

from jarvis.core.planner import Planner
from jarvis.runtime.execution_types import (
    ExecutionState,
    ExecutionStep,
    OpenIntent,
    OrchestratorStepIntent,
    PlayIntent,
    SearchIntent,
)


class ExecutionPlanner(Protocol):
    async def plan(self, text: str, state: ExecutionState) -> list[ExecutionStep]: ...
    async def replan(self, replan_input: dict[str, Any]) -> list[ExecutionStep]: ...


class RuntimePlanner:
    def __init__(self, core_planner: Planner, *, logger: logging.Logger | None = None) -> None:
        self._planner = core_planner
        self._logger = logger or logging.getLogger("Jarvis.RuntimePlanner")

    async def plan(self, text: str, state: ExecutionState) -> list[ExecutionStep]:
        # Phase 1.4: multi-plan generation (N=3) with temperature variation
        # Note: we import PlanScorer lazily to avoid circular imports
        # (CommandProcessor imports ExecutionPlanner from this module).
        from jarvis.runtime.command_processor import PlanScorer

        tier_hint_base = dict(state.context.get("tier_hint") or {})
        system_state = dict(state.context)
        memory = state.context.get("memory")
        conversation = state.context.get("conversation")

        temps = [0.2, 0.5, 0.8]
        async def _one(temp: float) -> Any:
            tier_hint = dict(tier_hint_base)
            tier_hint["temperature"] = temp
            return await self._planner.build_plan_async(
                text,
                memory=memory,
                conversation=conversation,
                system_state=system_state,
                tier_hint=tier_hint,
            )

        plans = await asyncio.gather(*[_one(t) for t in temps], return_exceptions=True)
        candidate_plans = [p for p in plans if not isinstance(p, Exception)]
        if not candidate_plans:
            for exc in (p for p in plans if isinstance(p, Exception)):
                self._logger.warning("Planner candidate generation failed: %s", exc)
            return []

        # Convert each candidate into runtime steps for scoring/selection.
        candidates: list[list[ExecutionStep]] = []
        for plan in candidate_plans:
            steps_raw = getattr(plan, "steps", []) or []
            candidates.append(self._to_runtime_steps(steps_raw))

        scorer = PlanScorer()
        scored = scorer.score_all(candidates, goal=text)
        state.context["planner_candidate_scores"] = scored

        # Select best candidate by score (tie-breaker: fewer steps).
        best_idx = 0
        best_score = -1.0
        best_steps = 10**9
        for i, row in enumerate(scored):
            s = float(row.get("score", 0.0) or 0.0)
            step_count = int(row.get("step_count", 0) or 0)
            if s > best_score or (s == best_score and step_count < best_steps):
                best_score = s
                best_steps = step_count
                best_idx = i

        self._logger.info(
            "Planner candidates scored. best_index=%d best_score=%.3f candidates=%s",
            best_idx + 1,
            best_score,
            scored,
        )

        # If only 1 plan generated (LLM consistency issue), proceed with it.
        return candidates[best_idx] if candidates else []

    async def replan(self, replan_input: dict[str, Any]) -> list[ExecutionStep]:
        # Preserve replan_input keys; runtime currently uses planner.replan only as optional hook.
        goal = str(replan_input.get("goal") or replan_input.get("system_state", {}).get("goal") or "").strip()
        if not goal:
            failed_step = replan_input.get("failed_step")
            goal = str(getattr(failed_step, "metadata", {}).get("source_text", "") or "").strip()
        if not goal:
            return []
        state = ExecutionState()
        state.context.update(dict(replan_input.get("system_state") or {}))
        return await self.plan(goal, state)

    @staticmethod
    def _to_runtime_steps(steps_raw: list[Any]) -> list[ExecutionStep]:
        # Phase 1.2: typed intent mapper based on action + params
        # Input: jarvis.core.context.ExecutionStep-like objects.
        created: list[ExecutionStep] = []
        by_step_id: dict[int, str] = {}

        # First pass: create steps + stable ids
        for raw in steps_raw:
            action = str(getattr(raw, "action", "") or "").lower().strip()
            params = getattr(raw, "params", None) or {}
            if not isinstance(params, dict):
                params = dict(params) if params is not None else {}
            target = str(getattr(raw, "target", "") or "").strip()

            intent = RuntimePlanner._map_intent(action=action, params=params, target=target, raw_step=raw)
            step = ExecutionStep(intent=intent, source="planner")
            confidence = getattr(raw, "confidence", None)
            if confidence is not None:
                step.metadata["confidence"] = float(confidence)
            created.append(step)

            try:
                step_id = int(getattr(raw, "step_id"))
                by_step_id[step_id] = step.id
            except Exception:
                pass

        # Second pass: map dependencies from core step_id integers -> runtime step ids
        for index, raw in enumerate(steps_raw):
            depends_on = getattr(raw, "depends_on", ()) or ()
            dep_ids: list[str] = []
            for dep in depends_on:
                try:
                    dep_ids.append(by_step_id[int(dep)])
                except Exception:
                    continue
            if dep_ids:
                created[index] = replace(created[index], dependencies=tuple(dep_ids))

        return created

    @staticmethod
    def _map_intent(*, action: str, params: dict[str, Any], target: str, raw_step: Any) -> Any:
        url = params.get("url") or params.get("target") or target
        query = params.get("query") or params.get("target") or target

        if action in {"open", "open_url", "navigate"}:
            return OpenIntent(target=str(url or "").strip())
        if action in {"search", "query"}:
            return SearchIntent(query=str(query or "").strip())
        if action in {"play", "play_video"}:
            platform = str(params.get("platform") or "youtube")
            return PlayIntent(query=str(query or "").strip(), platform=platform)
        return OrchestratorStepIntent(step_obj=raw_step)


__all__ = ["ExecutionPlanner", "RuntimePlanner"]

