from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.config.constants import (
    KNOWN_FOLDERS,
    KNOWN_URLS,
)
from jarvis.core.context import ToolDefinition
from jarvis.infrastructure.ai.online_provider import OnlineProvider
from jarvis.infrastructure.ai.intent_contracts import (
    IntentContractError,
    abort_reflection_payload,
    ask_user_reflection_payload,
    unknown_payload,
    validate_intent_summary_payload,
    validate_plan_payload,
    validate_reflection_payload,
)
from jarvis.infrastructure.ai.prompt_templates import (
    build_stage_system_prompt,
    build_stage_user_prompt,
)

logger = logging.getLogger("Jarvis.BrainClient")


@dataclass(slots=True)
class StageCallResult:
    stage_name: str
    request_payload: dict[str, Any]
    system_prompt: str
    user_prompt: str
    provider: str = ""
    raw_output_text: str = ""
    payload: dict[str, Any] | None = None
    error: str = ""


class LLMClient:
    """Structured multi-stage reasoning client used only by the brain module."""

    def __init__(self, provider: OnlineProvider | None = None) -> None:
        self._provider = provider or OnlineProvider()
        self._aliases = self._load_aliases()

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
        stage_result = self._query_stage(
            stage_name="intent_extraction",
            request_payload=self._build_stage_payload(
                stage_name="intent_extraction",
                user_text=user_text,
                normalized_text=normalized_text,
                tool_catalog=tool_catalog,
                memory=memory,
                conversation=conversation,
                system_state=system_state,
                tier_hint=tier_hint,
            ),
        )
        if stage_result.payload is None:
            if stage_result.raw_output_text.strip():
                fallback = unknown_payload(
                    clarification=(
                        "I couldn't turn that into a reliable intent yet. "
                        "Could you rephrase it a little more explicitly?"
                    ),
                    normalized_text=normalized_text,
                    unresolved_segments=[normalized_text] if normalized_text else [],
                )
                return self._attach_trace(stage_result, fallback, status="invalid_json", error=stage_result.error)
            fallback = unknown_payload(
                clarification=(
                    "I couldn't interpret that because the reasoning model is unavailable right now. "
                    "Please try again in a moment."
                ),
                normalized_text=normalized_text,
                unresolved_segments=[normalized_text] if normalized_text else [],
            )
            return self._attach_trace(stage_result, fallback, status="unavailable")

        try:
            validated = validate_intent_summary_payload(
                stage_result.payload,
                tool_catalog=tool_catalog,
                normalized_text=normalized_text,
            )
            logger.info("Brain stage intent_extraction: %s", json.dumps(validated, ensure_ascii=True))
            return self._attach_trace(stage_result, validated)
        except IntentContractError as exc:
            logger.warning("Intent extraction rejected model output: %s", exc)
            fallback = unknown_payload(
                clarification=(
                    "I couldn't turn that into a reliable intent yet. "
                    "Could you rephrase it a little more explicitly?"
                ),
                normalized_text=normalized_text,
                unresolved_segments=[normalized_text] if normalized_text else [],
            )
            return self._attach_trace(stage_result, fallback, status="rejected", error=str(exc))

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
        tier_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stage_result = self._query_stage(
            stage_name="task_planning",
            request_payload=self._build_stage_payload(
                stage_name="task_planning",
                user_text=user_text,
                normalized_text=normalized_text,
                tool_catalog=tool_catalog,
                memory=memory,
                conversation=conversation,
                system_state=system_state,
                tier_hint=tier_hint,
                intent_payload=intent_payload,
            ),
        )
        if stage_result.payload is None:
            if stage_result.raw_output_text.strip():
                fallback = unknown_payload(
                    clarification=(
                        "I couldn't build a valid execution plan for that yet. "
                        "Could you clarify the goal a little more?"
                    ),
                    normalized_text=normalized_text,
                    unresolved_segments=[normalized_text] if normalized_text else [],
                )
                return self._attach_trace(stage_result, fallback, status="invalid_json", error=stage_result.error)
            fallback = unknown_payload(
                clarification=(
                    "I understood the request, but I couldn't build a reliable plan because the reasoning model is unavailable."
                ),
                normalized_text=normalized_text,
                unresolved_segments=[normalized_text] if normalized_text else [],
            )
            return self._attach_trace(stage_result, fallback, status="unavailable")

        try:
            validated = validate_plan_payload(
                stage_result.payload,
                tool_catalog=tool_catalog,
                normalized_text=normalized_text,
            )
            logger.info("Brain stage task_planning: %s", json.dumps(validated, ensure_ascii=True))
            return self._attach_trace(stage_result, validated)
        except IntentContractError as exc:
            logger.warning("Task planning rejected model output: %s", exc)
            fallback = unknown_payload(
                clarification=(
                    "I couldn't build a valid execution plan for that yet. "
                    "Could you clarify the goal a little more?"
                ),
                normalized_text=normalized_text,
                unresolved_segments=[normalized_text] if normalized_text else [],
            )
            return self._attach_trace(stage_result, fallback, status="rejected", error=str(exc))

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
        tier_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stage_result = self._query_stage(
            stage_name="execution_reflection",
            request_payload=self._build_stage_payload(
                stage_name="execution_reflection",
                user_text=user_text,
                normalized_text=normalized_text,
                tool_catalog=tool_catalog,
                memory=memory,
                conversation=conversation,
                system_state=system_state,
                tier_hint=tier_hint,
                current_plan=current_plan,
                failed_step=failed_step,
                execution_result=execution_result,
            ),
        )
        if stage_result.payload is None:
            if stage_result.raw_output_text.strip():
                fallback = ask_user_reflection_payload(
                    question="That failed. Do you want me to try a different approach?",
                    normalized_text=normalized_text,
                )
                return self._attach_trace(stage_result, fallback, status="invalid_json", error=stage_result.error)
            fallback = abort_reflection_payload(
                message=(
                    "The reasoning model is unavailable, so I couldn't decide on a safe recovery."
                ),
                normalized_text=normalized_text,
            )
            return self._attach_trace(stage_result, fallback, status="unavailable")

        try:
            validated = validate_reflection_payload(
                stage_result.payload,
                tool_catalog=tool_catalog,
                normalized_text=normalized_text,
            )
            logger.info("Brain stage execution_reflection: %s", json.dumps(validated, ensure_ascii=True))
            return self._attach_trace(stage_result, validated)
        except IntentContractError as exc:
            logger.warning("Execution reflection rejected model output: %s", exc)
            fallback = ask_user_reflection_payload(
                question="That failed. Do you want me to try a different approach?",
                normalized_text=normalized_text,
            )
            return self._attach_trace(stage_result, fallback, status="rejected", error=str(exc))

    def parse_command(
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
        intent_payload = self.extract_intent(
            user_text=user_text,
            normalized_text=normalized_text,
            tool_catalog=tool_catalog,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
            tier_hint=tier_hint,
        )
        if intent_payload.get("clarification_question") or intent_payload.get("intent") == "unknown":
            return {
                **intent_payload,
                "steps": [],
            }
        return self.plan_task(
            user_text=user_text,
            normalized_text=normalized_text,
            tool_catalog=tool_catalog,
            intent_payload=intent_payload,
            memory=memory,
            conversation=conversation,
            system_state=system_state,
            tier_hint=tier_hint,
        )

    def _query_stage(
        self,
        *,
        stage_name: str,
        request_payload: dict[str, Any],
    ) -> StageCallResult:
        serialized_request = json.dumps(request_payload, ensure_ascii=True)
        system_prompt = build_stage_system_prompt(stage_name)
        user_prompt = build_stage_user_prompt(
            stage_name=stage_name,
            request_payload=serialized_request,
        )
        logger.info(
            "Brain request prepared.",
            extra={
                "event": "llm.stage.request",
                "stage": stage_name,
                "payload": request_payload,
            },
        )
        try:
            response = self._provider.query(
                user_prompt,
                system_prompt=system_prompt,
                temperature=0.0,
                response_format="json_object",
            )
        except Exception as exc:
            logger.exception(
                "Brain stage %s crashed before a response was returned.",
                stage_name,
                extra={"event": "llm.stage.crashed", "stage": stage_name},
            )
            return StageCallResult(
                stage_name=stage_name,
                request_payload=request_payload,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                error=str(exc),
            )
        result = StageCallResult(
            stage_name=stage_name,
            request_payload=request_payload,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            provider=response.provider,
            raw_output_text=response.text,
            error=str(response.error or ""),
        )
        if not response.success or not response.text.strip():
            logger.warning(
                "Brain stage unavailable.",
                extra={
                    "event": "llm.stage.unavailable",
                    "stage": stage_name,
                    "provider": response.provider,
                    "error": response.error,
                },
            )
            return result

        logger.info(
            "Brain raw output received.",
            extra={
                "event": "llm.stage.response",
                "stage": stage_name,
                "provider": response.provider,
                "raw_output": response.text,
            },
        )
        logger.debug(f"[Brain] Raw LLM output: {response.text}")
        try:
            result.payload = self._extract_json_payload(response.text)
            return result
        except ValueError as exc:
            logger.warning(
                "Brain stage returned invalid JSON.",
                extra={
                    "event": "llm.stage.invalid_json",
                    "stage": stage_name,
                    "provider": response.provider,
                    "error": str(exc),
                },
            )
            result.error = str(exc)
            return result

    def _build_stage_payload(
        self,
        *,
        stage_name: str,
        user_text: str,
        normalized_text: str,
        tool_catalog: tuple[ToolDefinition, ...],
        memory: Any | None,
        conversation: list[dict[str, str]] | None,
        system_state: dict[str, Any] | None,
        tier_hint: dict[str, Any] | None = None,
        intent_payload: dict[str, Any] | None = None,
        current_plan: dict[str, Any] | None = None,
        failed_step: dict[str, Any] | None = None,
        execution_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = memory.snapshot() if hasattr(memory, "snapshot") else memory
        payload = {
            "stage": stage_name,
            "request": {
                "user_text": user_text,
                "normalized_text": normalized_text,
            },
            "allowed_tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "required_args": list(tool.required_params),
                    "risk": tool.risk,
                    "preferred_surface": tool.preferred_surface,
                    "opens_surface": tool.opens_surface,
                    "examples": list(tool.examples),
                }
                for tool in tool_catalog
            ],
            "resource_hints": {
                "known_urls": KNOWN_URLS,
                "known_folders": sorted(KNOWN_FOLDERS),
                "aliases": self._aliases,
            },
            "session_context": {
                "last_action": self._snapshot_value(snapshot, "last_command"),
                "last_target": self._snapshot_value(snapshot, "last_target"),
                "last_app": self._snapshot_value(snapshot, "last_app"),
                "last_goal": self._snapshot_value(snapshot, "last_goal"),
                "last_params": dict(self._snapshot_value(snapshot, "last_params") or {}),
                "recent_contexts": self._recent_contexts(snapshot),
            },
            "memory_context": {
                "short_term": list(self._snapshot_value(snapshot, "short_term") or []),
                "long_term": dict(self._snapshot_value(snapshot, "long_term") or {}),
                "semantic": list(self._snapshot_value(snapshot, "semantic") or []),
                "write_policy": dict(self._snapshot_value(snapshot, "write_policy") or {}),
            },
            "conversation_context": list(conversation or [])[-6:],
            "system_state": dict(system_state or {}),
            "tier_hint": dict(tier_hint or {}),
        }
        if intent_payload is not None:
            payload["intent_summary"] = dict(intent_payload)
        if current_plan is not None:
            payload["current_plan"] = dict(current_plan)
        if failed_step is not None:
            payload["failed_step"] = dict(failed_step)
        if execution_result is not None:
            payload["execution_result"] = dict(execution_result)
        return payload

    @staticmethod
    def _attach_trace(
        stage_result: StageCallResult,
        payload: dict[str, Any],
        *,
        status: str = "success",
        error: str = "",
    ) -> dict[str, Any]:
        trace = {
            "stage": stage_result.stage_name,
            "status": status,
            "provider": stage_result.provider,
            "request_payload": dict(stage_result.request_payload),
            "raw_output": dict(stage_result.payload or {}),
            "normalized_output": dict(payload),
            "error": str(error or stage_result.error or "").strip(),
        }
        enriched = dict(payload)
        existing = list(enriched.get("_reasoning_trace") or [])
        existing.append(trace)
        enriched["_reasoning_trace"] = existing
        return enriched

    @staticmethod
    def _extract_json_payload(text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.removeprefix("json").strip()

        try:
            decoded = json.loads(cleaned)
        except json.JSONDecodeError:
            decoded = json.loads(LLMClient._extract_json_fragment(cleaned))
        if not isinstance(decoded, dict):
            raise ValueError("Model response must be a JSON object.")
        return decoded

    @staticmethod
    def _extract_json_fragment(text: str) -> str:
        object_start = text.find("{")
        object_end = text.rfind("}")
        if object_start == -1 or object_end <= object_start:
            raise ValueError("No JSON object found in model response.")
        return text[object_start : object_end + 1]

    @staticmethod
    def _snapshot_value(snapshot: Any, key: str) -> Any:
        if snapshot is None:
            return None
        if isinstance(snapshot, dict):
            return snapshot.get(key)
        return getattr(snapshot, key, None)

    def _recent_contexts(self, snapshot: Any | None) -> list[dict[str, Any]]:
        if snapshot is None:
            recent_contexts = []
        elif isinstance(snapshot, dict):
            recent_contexts = list(snapshot.get("recent_contexts", ()) or [])
        else:
            recent_contexts = list(getattr(snapshot, "recent_contexts", ()) or [])
        payload: list[dict[str, Any]] = []
        for record in recent_contexts[:5]:
            payload.append(
                {
                    "action": str(getattr(record, "action", "")).strip(),
                    "target": str(getattr(record, "target", "")).strip(),
                    "primary_app": str(getattr(record, "primary_app", "")).strip(),
                    "params": dict(getattr(record, "params", {}) or {}),
                    "age_seconds": float(getattr(record, "age_seconds", 0.0) or 0.0),
                }
            )
        return payload

    @staticmethod
    def _load_aliases() -> dict[str, str]:
        alias_path = Path(__file__).resolve().parents[3] / "config" / "aliases.json"
        if alias_path.exists():
            try:
                payload = json.loads(alias_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                return {
                    " ".join(str(alias).strip().lower().split()): " ".join(str(target).strip().lower().split())
                    for alias, target in payload.items()
                    if str(alias).strip() and str(target).strip()
                }
        return {key: key for key in KNOWN_URLS}
