from __future__ import annotations

import asyncio
from typing import Any

from jarvis.core.brain import Brain
from jarvis.core.context import ExecutionPlan, ExecutionStep, PlanPreview, ReflectionDecision, ToolDefinition


class Planner:
    """Pure LLM-plan validator and normalizer.

    The planner does not reinterpret the request. It only converts validated
    structured LLM directives into an executable plan and can optionally apply
    stable ordering by declared dependencies.
    """

    def __init__(
        self,
        *,
        brain: Brain,
        tool_catalog: tuple[ToolDefinition, ...],
    ) -> None:
        self._brain = brain
        self._tool_catalog = tuple(tool_catalog)
        self._tool_names = {tool.name for tool in self._tool_catalog}

    def parse(
        self,
        text: str,
        *,
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
        tier_hint: dict[str, Any] | None = None,
    ) -> PlanPreview:
        decision = self._brain.understand(
            text,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
            tier_hint=tier_hint,
        )
        directives = [directive for directive in decision.directives if directive.action in self._tool_names]
        return PlanPreview(
            directives=directives,
            confidence=decision.confidence,
            clarification_question=decision.clarification_question,
            normalized_text=decision.normalized_text,
            unresolved_segments=list(decision.unresolved_segments),
            reasoning_trace=decision.reasoning_trace,
        )

    async def parse_async(
        self,
        text: str,
        *,
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
        tier_hint: dict[str, Any] | None = None,
    ) -> PlanPreview:
        return await asyncio.to_thread(
            self.parse,
            text,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
            tier_hint=tier_hint,
        )

    def build_plan(
        self,
        goal: str,
        *,
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
        tier_hint: dict[str, Any] | None = None,
    ) -> ExecutionPlan:
        preview = self.parse(goal, memory=memory, conversation=conversation, system_state=system_state, tier_hint=tier_hint)
        if preview.clarification_question:
            return ExecutionPlan(
                intent="planner_message",
                goal=goal,
                fallback_response=preview.clarification_question,
                clarification_question=preview.clarification_question,
                confidence=preview.confidence,
                reasoning_trace=preview.reasoning_trace,
            )
        if not preview.directives:
            return ExecutionPlan(
                intent="planner_message",
                goal=goal,
                fallback_response="I could not turn that into a reliable tool plan yet.",
                confidence=preview.confidence,
                reasoning_trace=preview.reasoning_trace,
            )

        normalized_steps: list[ExecutionStep] = []
        for index, directive in enumerate(preview.directives, start=1):
            normalized_steps.append(
                ExecutionStep(
                    action=directive.action,
                    step_id=index,
                    target=directive.target,
                    params=dict(directive.params),
                    depends_on=tuple(directive.depends_on),
                    condition=directive.condition,
                    param_bindings=dict(directive.param_bindings),
                    description=directive.description,
                    confidence=directive.confidence,
                )
            )

        intent = normalized_steps[0].action if len(normalized_steps) == 1 else "multi_step_command"
        return ExecutionPlan(
            intent=intent,
            goal=goal,
            steps=self._ordered_steps(normalized_steps),
            confidence=preview.confidence,
            reasoning_trace=preview.reasoning_trace,
        )

    async def build_plan_async(
        self,
        goal: str,
        *,
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
        tier_hint: dict[str, Any] | None = None,
    ) -> ExecutionPlan:
        if hasattr(self._brain, "_llm_is_reachable"):
            reachable = await self._brain._llm_is_reachable()
            if not reachable:
                return ExecutionPlan(
                    intent="error",
                    goal=goal,
                    fallback_response="I can't reach my reasoning engine right now. Please check that your LLM model is running.",
                    clarification_question="I can't reach my reasoning engine right now. Please check that your LLM model is running.",
                    confidence=0.0
                )

        return await asyncio.to_thread(
            self.build_plan,
            goal,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
            tier_hint=tier_hint,
        )

    def reflect(
        self,
        goal: str,
        *,
        current_plan: dict[str, Any],
        failed_step: dict[str, Any],
        execution_result: dict[str, Any],
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
    ) -> ReflectionDecision:
        reflection = self._brain.reflect(
            goal,
            current_plan=current_plan,
            failed_step=failed_step,
            execution_result=execution_result,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
        )
        reflection.directives = [directive for directive in reflection.directives if directive.action in self._tool_names]
        return reflection

    @staticmethod
    def _ordered_steps(steps: list[ExecutionStep]) -> list[ExecutionStep]:
        # The LLM remains the source of truth. We only stabilize ordering by step_id.
        return sorted(steps, key=lambda step: int(step.step_id))
