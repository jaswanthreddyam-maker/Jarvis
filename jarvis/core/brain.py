from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from jarvis.core.context import (
    ActionDirective,
    BrainDecision,
    ReasoningStageTrace,
    ReflectionDecision,
    ToolDefinition,
)

logger = logging.getLogger("Jarvis.Brain")


class BrainModelClient(Protocol):
    def extract_intent(
        self,
        *,
        user_text: str,
        normalized_text: str,
        tool_catalog: tuple[ToolDefinition, ...],
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
        tier_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    def plan_task(
        self,
        *,
        user_text: str,
        normalized_text: str,
        tool_catalog: tuple[ToolDefinition, ...],
        intent_payload: dict[str, Any],
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    def reflect_execution(
        self,
        *,
        user_text: str,
        normalized_text: str,
        tool_catalog: tuple[ToolDefinition, ...],
        current_plan: dict[str, Any],
        failed_step: dict[str, Any],
        execution_result: dict[str, Any],
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


class Brain:
    """Central LLM reasoning module for intent extraction, planning, and reflection."""

    def __init__(self, *, llm_client: BrainModelClient, tool_catalog: tuple[ToolDefinition, ...]) -> None:
        self._llm_client = llm_client
        self._tool_catalog = tuple(tool_catalog)
        self._tool_names = {tool.name for tool in self._tool_catalog}

    @property
    def tool_catalog(self) -> tuple[ToolDefinition, ...]:
        return self._tool_catalog

    async def _llm_is_reachable(self) -> bool:
        try:
            import httpx
            url = getattr(self._llm_client, "base_url", None)
            if not url:
                url = "http://127.0.0.1:11434/"
            if "openai.com" in url or "anthropic.com" in url:
                return True
            async with httpx.AsyncClient() as client:
                r = await client.get(url, timeout=2.0)
                return r.status_code == 200
        except Exception:
            return False

    def extract_intent(
        self,
        text: str,
        *,
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
        tier_hint: dict[str, Any] | None = None,
    ) -> BrainDecision:
        normalized = self._normalize_request(text)
        if not normalized:
            return BrainDecision(normalized_text=normalized)

        payload = self._llm_client.extract_intent(
            user_text=text,
            normalized_text=normalized,
            tool_catalog=self._tool_catalog,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
            tier_hint=tier_hint,
        )
        logger.info("Brain intent summary: %s", payload)
        return self._coerce_decision(payload, normalized_text=normalized)

    def understand(
        self,
        text: str,
        *,
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
        tier_hint: dict[str, Any] | None = None,
    ) -> BrainDecision:
        normalized = self._normalize_request(text)
        if not normalized:
            return BrainDecision(normalized_text=normalized)

        intent_decision = self.extract_intent(
            text,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
            tier_hint=tier_hint,
        )
        if intent_decision.clarification_question or intent_decision.intent in {"", "unknown"}:
            return intent_decision

        planning_payload = self._llm_client.plan_task(
            user_text=text,
            normalized_text=normalized,
            tool_catalog=self._tool_catalog,
            intent_payload={
                "intent": intent_decision.intent,
                "tool": intent_decision.tool,
                "args": dict(intent_decision.args),
                "confidence": intent_decision.confidence,
                "clarification_question": intent_decision.clarification_question,
                "response": intent_decision.response,
                "unresolved_segments": list(intent_decision.unresolved_segments),
                "source": intent_decision.source,
            },
            memory=memory,
            conversation=conversation,
            system_state=system_state,
        )
        logger.info("Brain planning output: %s", planning_payload)
        plan_decision = self._coerce_decision(planning_payload, normalized_text=normalized)
        plan_trace = plan_decision.reasoning_trace
        if intent_decision.reasoning_trace:
            plan_decision.reasoning_trace = tuple(intent_decision.reasoning_trace) + tuple(plan_trace)
        if not plan_decision.intent:
            plan_decision.intent = intent_decision.intent
        if not plan_decision.tool:
            plan_decision.tool = intent_decision.tool
        if not plan_decision.args:
            plan_decision.args = dict(intent_decision.args)
        if not plan_decision.response:
            plan_decision.response = intent_decision.response
        if not plan_decision.clarification_question:
            plan_decision.clarification_question = intent_decision.clarification_question
        return plan_decision

    def reflect(
        self,
        text: str,
        *,
        current_plan: dict[str, Any],
        failed_step: dict[str, Any],
        execution_result: dict[str, Any],
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
    ) -> ReflectionDecision:
        normalized = self._normalize_request(text)
        payload = self._llm_client.reflect_execution(
            user_text=text,
            normalized_text=normalized,
            tool_catalog=self._tool_catalog,
            current_plan=current_plan,
            failed_step=failed_step,
            execution_result=execution_result,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
        )
        logger.info("Brain reflection output: %s", payload)
        return self._coerce_reflection(payload, normalized_text=normalized)

    async def understand_async(
        self,
        text: str,
        *,
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
        tier_hint: dict[str, Any] | None = None,
    ) -> BrainDecision:
        return await asyncio.to_thread(
            self.understand,
            text,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
            tier_hint=tier_hint,
        )

    async def reflect_async(
        self,
        text: str,
        *,
        current_plan: dict[str, Any],
        failed_step: dict[str, Any],
        execution_result: dict[str, Any],
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
    ) -> ReflectionDecision:
        return await asyncio.to_thread(
            self.reflect,
            text,
            current_plan=current_plan,
            failed_step=failed_step,
            execution_result=execution_result,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
        )

    def _coerce_decision(self, payload: dict[str, Any], *, normalized_text: str) -> BrainDecision:
        directives = self._coerce_directives(payload)
        clarification = (
            str(payload.get("clarification_question", "") or payload.get("response", "") or "").strip() or None
        )
        unresolved = [str(item).strip() for item in list(payload.get("unresolved_segments") or []) if str(item).strip()]
        confidence = float(payload.get("confidence", 0.0) or 0.0) or self._average_confidence(directives)
        return BrainDecision(
            intent=str(payload.get("intent", "")).strip(),
            tool=str(payload.get("tool", "")).strip(),
            args=dict(payload.get("args") or {}),
            directives=directives,
            confidence=confidence,
            response=str(payload.get("response", "") or "").strip(),
            clarification_question=clarification,
            normalized_text=str(payload.get("normalized_text", normalized_text) or normalized_text),
            unresolved_segments=unresolved,
            source=str(payload.get("source", "brain") or "brain"),
            reasoning_trace=self._coerce_reasoning_trace(payload),
        )

    def _coerce_reflection(self, payload: dict[str, Any], *, normalized_text: str) -> ReflectionDecision:
        directives = self._coerce_directives(payload)
        unresolved = [str(item).strip() for item in list(payload.get("unresolved_segments") or []) if str(item).strip()]
        return ReflectionDecision(
            decision=str(payload.get("decision", "abort") or "abort").strip(),
            directives=directives,
            confidence=float(payload.get("confidence", 0.0) or 0.0) or self._average_confidence(directives),
            message=str(payload.get("message", "") or "").strip(),
            question=str(payload.get("question", "") or "").strip() or None,
            normalized_text=str(payload.get("normalized_text", normalized_text) or normalized_text),
            unresolved_segments=unresolved,
            source=str(payload.get("source", "brain") or "brain"),
            reasoning_trace=self._coerce_reasoning_trace(payload),
        )

    @staticmethod
    def _coerce_reasoning_trace(payload: dict[str, Any]) -> tuple[ReasoningStageTrace, ...]:
        traces: list[ReasoningStageTrace] = []
        raw_trace = list(payload.get("_reasoning_trace") or payload.get("reasoning_trace") or [])
        for item in raw_trace:
            if not isinstance(item, dict):
                continue
            traces.append(
                ReasoningStageTrace(
                    stage=str(item.get("stage", "")).strip(),
                    status=str(item.get("status", "success") or "success").strip(),
                    provider=str(item.get("provider", "") or "").strip(),
                    request_payload=dict(item.get("request_payload") or {}),
                    raw_output=dict(item.get("raw_output") or {}),
                    normalized_output=dict(item.get("normalized_output") or {}),
                    error=str(item.get("error", "") or "").strip(),
                )
            )
        return tuple(traces)

    def _coerce_directives(self, payload: dict[str, Any]) -> list[ActionDirective]:
        directives: list[ActionDirective] = []
        raw_steps = list(payload.get("steps", []))
        if not raw_steps and payload.get("tool"):
            raw_steps = [
                {
                    "tool": payload.get("tool"),
                    "target": payload.get("target", ""),
                    "args": payload.get("args", {}),
                    "description": payload.get("description", ""),
                    "confidence": payload.get("confidence", 0.8),
                    "depends_on": [],
                    "condition": None,
                    "param_bindings": payload.get("param_bindings", {}),
                    "source": payload.get("source", "brain"),
                }
            ]

        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                continue
            action = str(raw_step.get("tool", "") or raw_step.get("action", "")).strip()
            if not action or action not in self._tool_names:
                continue
            params = dict(raw_step.get("args") or raw_step.get("params") or {})
            target = str(raw_step.get("target", "")).strip()
            directives.append(
                ActionDirective(
                    action=action,
                    target=target,
                    params=params,
                    description=str(raw_step.get("description", "")).strip() or self._default_description(action, target),
                    confidence=float(raw_step.get("confidence", 0.8) or 0.8),
                    depends_on=list(raw_step.get("depends_on", [])),
                    condition=raw_step.get("condition"),
                    param_bindings={
                        str(key): str(value)
                        for key, value in dict(raw_step.get("param_bindings") or {}).items()
                        if str(key).strip() and str(value).strip()
                    },
                    source=str(raw_step.get("source", payload.get("source", "brain")) or "brain"),
                )
            )
        return directives

    @staticmethod
    def _normalize_request(text: str) -> str:
        return " ".join(text.strip().lower().split())

    @staticmethod
    def _average_confidence(directives: list[ActionDirective]) -> float:
        if not directives:
            return 0.0
        return sum(directive.confidence for directive in directives) / len(directives)

    @staticmethod
    def _default_description(action: str, target: str) -> str:
        return {
            "open_app": f"Open the {target} application.",
            "open_url": f"Open {target} in the browser.",
            "open_explorer": f"Open the {target} folder.",
            "search_web": f"Search the web for {target}.",
            "search_youtube": f"Search YouTube for {target}.",
            "focus_app": f"Focus the {target} application.",
            "close_app": f"Close {target}.",
            "create_file": f"Create the file {target}.",
        }.get(action, action.replace("_", " ").strip())
