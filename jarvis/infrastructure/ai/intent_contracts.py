from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.core.context import ToolDefinition


class IntentContractError(ValueError):
    """Raised when a model response does not satisfy a reasoning contract."""


class IntentSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: str
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    clarification_question: str | None = None
    response: str | None = None
    unresolved_segments: list[str] = Field(default_factory=list)
    source: str = "llm"


class IntentStepPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    target: str = ""
    description: str = ""
    confidence: float = 0.0
    depends_on: list[int] = Field(default_factory=list)
    condition: str | None = None
    param_bindings: dict[str, str] = Field(default_factory=dict)


class PlanningPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: str
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    clarification_question: str | None = None
    response: str | None = None
    unresolved_segments: list[str] = Field(default_factory=list)
    steps: list[IntentStepPayload] = Field(default_factory=list)
    source: str = "llm"


class ReflectionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision: str
    confidence: float = 0.0
    message: str | None = None
    question: str | None = None
    unresolved_segments: list[str] = Field(default_factory=list)
    steps: list[IntentStepPayload] = Field(default_factory=list)
    source: str = "llm"


def validate_intent_summary_payload(
    raw_payload: dict[str, Any],
    *,
    tool_catalog: tuple[ToolDefinition, ...],
    normalized_text: str,
) -> dict[str, Any]:
    try:
        parsed = IntentSummaryPayload.model_validate(raw_payload)
    except ValidationError as exc:
        raise IntentContractError(f"Intent summary validation failed: {exc}") from exc

    tool_map = {tool.name: tool for tool in tool_catalog}
    normalized_intent = _normalize_identifier(parsed.intent)
    if normalized_intent in {"", "unknown", "clarification_needed"}:
        clarification = _clean_text(parsed.clarification_question) or _clean_text(parsed.response)
        return unknown_payload(
            clarification=clarification or "Can you clarify what you want me to do?",
            normalized_text=normalized_text,
            unresolved_segments=parsed.unresolved_segments,
            confidence=max(0.0, min(1.0, float(parsed.confidence or 0.0))),
            source=parsed.source,
        )

    tool_name = _normalize_identifier(parsed.tool)
    args = _sanitize_object(parsed.args)
    if tool_name:
        tool_definition = tool_map.get(tool_name)
        if tool_definition is None:
            raise IntentContractError(f"Unknown tool '{parsed.tool}' in intent summary.")
        missing = [
            required_param
            for required_param in tool_definition.required_params
            if _is_missing(args.get(required_param))
        ]
        if missing:
            return unknown_payload(
                clarification=_build_missing_arg_question(tool_definition, missing),
                normalized_text=normalized_text,
                unresolved_segments=[tool_name],
                confidence=max(0.0, min(1.0, float(parsed.confidence or 0.0))),
                source=parsed.source,
            )

    return {
        "intent": normalized_intent,
        "tool": tool_name,
        "args": args,
        "confidence": max(0.0, min(1.0, float(parsed.confidence or 0.0))),
        "clarification_question": _clean_text(parsed.clarification_question) or None,
        "response": _clean_text(parsed.response) or "",
        "unresolved_segments": [_clean_text(item) for item in parsed.unresolved_segments if _clean_text(item)],
        "normalized_text": normalized_text,
        "source": _clean_text(parsed.source) or "llm",
    }


def validate_plan_payload(
    raw_payload: dict[str, Any],
    *,
    tool_catalog: tuple[ToolDefinition, ...],
    normalized_text: str,
) -> dict[str, Any]:
    try:
        parsed = PlanningPayload.model_validate(raw_payload)
    except ValidationError as exc:
        raise IntentContractError(f"Planning payload validation failed: {exc}") from exc

    normalized_intent = _normalize_identifier(parsed.intent)
    if normalized_intent in {"", "unknown", "clarification_needed"}:
        clarification = _clean_text(parsed.clarification_question) or _clean_text(parsed.response)
        return unknown_payload(
            clarification=clarification or "Can you clarify what you want me to do?",
            normalized_text=normalized_text,
            unresolved_segments=parsed.unresolved_segments,
            confidence=max(0.0, min(1.0, float(parsed.confidence or 0.0))),
            source=parsed.source,
        )

    validated_steps = _validate_steps(
        list(parsed.steps),
        tool_catalog=tool_catalog,
        fallback_tool=parsed.tool,
        fallback_args=parsed.args,
        fallback_confidence=parsed.confidence,
        normalized_text=normalized_text,
        source=parsed.source,
    )

    overall_intent = normalized_intent or (
        validated_steps[0]["tool"] if len(validated_steps) == 1 else "multi_step_command"
    )
    single_step = validated_steps[0] if len(validated_steps) == 1 else None
    return {
        "intent": overall_intent,
        "tool": single_step["tool"] if single_step else "",
        "args": dict(single_step["args"]) if single_step else {},
        "confidence": max(0.0, min(1.0, float(parsed.confidence or _average_confidence(validated_steps)))),
        "clarification_question": _clean_text(parsed.clarification_question) or None,
        "response": _clean_text(parsed.response) or "",
        "steps": validated_steps,
        "unresolved_segments": [_clean_text(item) for item in parsed.unresolved_segments if _clean_text(item)],
        "normalized_text": normalized_text,
        "source": _clean_text(parsed.source) or "llm",
    }


def validate_intent_payload(
    raw_payload: dict[str, Any],
    *,
    tool_catalog: tuple[ToolDefinition, ...],
    normalized_text: str,
) -> dict[str, Any]:
    return validate_plan_payload(
        raw_payload,
        tool_catalog=tool_catalog,
        normalized_text=normalized_text,
    )


def validate_reflection_payload(
    raw_payload: dict[str, Any],
    *,
    tool_catalog: tuple[ToolDefinition, ...],
    normalized_text: str,
) -> dict[str, Any]:
    try:
        parsed = ReflectionPayload.model_validate(raw_payload)
    except ValidationError as exc:
        raise IntentContractError(f"Reflection payload validation failed: {exc}") from exc

    decision = _normalize_identifier(parsed.decision)
    if decision in {"", "unknown", "clarification_needed"}:
        decision = "ask_user"

    if decision in {"retry", "replan", "fallback"}:
        validated_steps = _validate_steps(
            list(parsed.steps),
            tool_catalog=tool_catalog,
            normalized_text=normalized_text,
            source=parsed.source,
        )
        return {
            "decision": "retry" if decision == "fallback" else decision,
            "confidence": max(0.0, min(1.0, float(parsed.confidence or _average_confidence(validated_steps)))),
            "message": _clean_text(parsed.message),
            "question": _clean_text(parsed.question) or None,
            "steps": validated_steps,
            "unresolved_segments": [_clean_text(item) for item in parsed.unresolved_segments if _clean_text(item)],
            "normalized_text": normalized_text,
            "source": _clean_text(parsed.source) or "llm",
        }

    if decision == "ask_user":
        question = _clean_text(parsed.question) or _clean_text(parsed.message)
        return {
            "decision": "ask_user",
            "confidence": max(0.0, min(1.0, float(parsed.confidence or 0.0))),
            "message": _clean_text(parsed.message),
            "question": question or "Can you clarify what you want me to do next?",
            "steps": [],
            "unresolved_segments": [_clean_text(item) for item in parsed.unresolved_segments if _clean_text(item)],
            "normalized_text": normalized_text,
            "source": _clean_text(parsed.source) or "llm",
        }

    return {
        "decision": "abort",
        "confidence": max(0.0, min(1.0, float(parsed.confidence or 0.0))),
        "message": _clean_text(parsed.message) or "I couldn't find a safe recovery for that failure.",
        "question": _clean_text(parsed.question) or None,
        "steps": [],
        "unresolved_segments": [_clean_text(item) for item in parsed.unresolved_segments if _clean_text(item)],
        "normalized_text": normalized_text,
        "source": _clean_text(parsed.source) or "llm",
    }


def unknown_payload(
    *,
    clarification: str,
    normalized_text: str,
    unresolved_segments: list[str] | None = None,
    confidence: float = 0.0,
    source: str = "llm",
) -> dict[str, Any]:
    cleaned_clarification = _clean_text(clarification) or "Can you clarify what you want me to do?"
    return {
        "intent": "unknown",
        "tool": "",
        "args": {},
        "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
        "clarification_question": cleaned_clarification,
        "response": cleaned_clarification,
        "steps": [],
        "unresolved_segments": [_clean_text(item) for item in list(unresolved_segments or []) if _clean_text(item)],
        "normalized_text": normalized_text,
        "source": _clean_text(source) or "llm",
    }


def abort_reflection_payload(
    *,
    message: str,
    normalized_text: str,
    confidence: float = 0.0,
    source: str = "llm",
) -> dict[str, Any]:
    cleaned_message = _clean_text(message) or "I couldn't find a safe recovery for that failure."
    return {
        "decision": "abort",
        "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
        "message": cleaned_message,
        "question": None,
        "steps": [],
        "unresolved_segments": [],
        "normalized_text": normalized_text,
        "source": _clean_text(source) or "llm",
    }


def ask_user_reflection_payload(
    *,
    question: str,
    normalized_text: str,
    confidence: float = 0.0,
    source: str = "llm",
) -> dict[str, Any]:
    cleaned_question = _clean_text(question) or "Can you clarify what you want me to do next?"
    return {
        "decision": "ask_user",
        "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
        "message": cleaned_question,
        "question": cleaned_question,
        "steps": [],
        "unresolved_segments": [],
        "normalized_text": normalized_text,
        "source": _clean_text(source) or "llm",
    }


def _validate_steps(
    raw_steps: list[IntentStepPayload],
    *,
    tool_catalog: tuple[ToolDefinition, ...],
    normalized_text: str,
    source: str,
    fallback_tool: str = "",
    fallback_args: dict[str, Any] | None = None,
    fallback_confidence: float = 0.0,
) -> list[dict[str, Any]]:
    tool_map = {tool.name: tool for tool in tool_catalog}
    steps = list(raw_steps)
    if not steps and _normalize_identifier(fallback_tool):
        steps = [
            IntentStepPayload(
                tool=fallback_tool,
                args=dict(fallback_args or {}),
                confidence=fallback_confidence,
            )
        ]
    if not steps:
        raise IntentContractError("Planning payload did not include any execution steps.")

    validated_steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        tool_name = _normalize_identifier(step.tool)
        tool_definition = tool_map.get(tool_name)
        if tool_definition is None:
            raise IntentContractError(f"Unknown tool '{step.tool}' in step {index}.")

        args = _sanitize_object(step.args)
        missing = [
            required_param
            for required_param in tool_definition.required_params
            if _is_missing(args.get(required_param))
        ]
        if missing:
            raise IntentContractError(
                _build_missing_arg_question(tool_definition, missing)
            )

        target = _clean_text(step.target) or _derive_target(args)
        description = _clean_text(step.description) or _default_description(tool_name, target or tool_name)
        validated_steps.append(
            {
                "tool": tool_name,
                "target": target,
                "args": args,
                "description": description,
                "confidence": max(0.0, min(1.0, float(step.confidence or fallback_confidence or 0.8))),
                "depends_on": list(step.depends_on),
                "condition": step.condition,
                "permission_level": getattr(tool_definition, "risk", "safe"),
                "param_bindings": {
                    _normalize_identifier(name): _clean_text(binding)
                    for name, binding in step.param_bindings.items()
                    if _normalize_identifier(name) and _clean_text(binding)
                },
            }
        )
    return validated_steps


def _sanitize_object(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "").strip()
    if isinstance(value, list):
        return [_sanitize_object(item) for item in value]
    if isinstance(value, dict):
        return {
            _clean_text(str(key)): _sanitize_object(item)
            for key, item in value.items()
            if _clean_text(str(key))
        }
    return value


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _derive_target(args: dict[str, Any]) -> str:
    for key in (
        "query",
        "url",
        "app_name",
        "name",
        "path",
        "title",
        "message",
        "content",
        "action_type",
        "value",
    ):
        candidate = args.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _build_missing_arg_question(tool: ToolDefinition, missing: list[str]) -> str:
    required = ", ".join(missing)
    if len(missing) == 1:
        return f"I think you want '{tool.name}', but I still need '{required}'."
    return f"I think you want '{tool.name}', but I still need these fields: {required}."


def _average_confidence(steps: list[dict[str, Any]]) -> float:
    if not steps:
        return 0.0
    return sum(float(step.get("confidence", 0.0) or 0.0) for step in steps) / len(steps)


def _normalize_identifier(value: str) -> str:
    return " ".join(value.strip().lower().split()).replace(" ", "_")


def _clean_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())


def _default_description(action: str, target: str) -> str:
    return {
        "open_app": f"Open the {target} application.",
        "focus_app": f"Focus the {target} application.",
        "close_app": f"Close the {target} application.",
        "open_url": f"Open {target} in the browser.",
        "search_web": f"Search the web for {target}.",
        "search_youtube": f"Search YouTube for {target}.",
        "play_youtube": f"Play YouTube content for {target}.",
        "create_file": f"Create the file {target}.",
        "overwrite_file": f"Overwrite the file {target}.",
        "read_file": f"Read the file {target}.",
        "delete_file": f"Delete the file {target}.",
        "open_explorer": f"Open the {target} folder.",
        "set_volume": f"Adjust the volume to {target}.",
        "set_clipboard": "Update the clipboard.",
        "get_clipboard": "Read the clipboard.",
        "system_action": f"Run the system action {target}.",
        "remember_fact": f"Store the memory '{target}'.",
        "recall_memory": f"Recall memory about {target}.",
        "set_reminder": f"Schedule a reminder for {target}.",
        "switch_window": f"Switch to the {target} window.",
        "close_active_window": "Close the active window.",
        "minimize_window": "Minimize the active window.",
        "install_app": f"Install {target}.",
        "get_time": "Get the local time.",
        "health_check": "Check runtime health.",
        "report_capabilities": "Report current capabilities.",
    }.get(action, action.replace("_", " ").strip())
