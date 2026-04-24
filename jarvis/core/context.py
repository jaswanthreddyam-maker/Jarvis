from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    required_params: tuple[str, ...] = field(default_factory=tuple)
    examples: tuple[str, ...] = field(default_factory=tuple)
    verification: str = ""
    risk: str = "safe"
    context_param: str = ""
    preferred_surface: str = ""
    opens_surface: str = ""
    prelude_action: str = ""
    prelude_target: str = ""
    prelude_params: dict[str, Any] = field(default_factory=dict)
    prelude_description: str = ""

    def prompt_line(self) -> str:
        required = ", ".join(self.required_params) if self.required_params else "none"
        examples = "; ".join(self.examples[:2]) if self.examples else "none"
        return (
            f"{self.name}: {self.description} "
            f"Required args: {required}. Risk: {self.risk}. Example: {examples}"
        )


@dataclass(slots=True)
class ActionDirective:
    action: str
    target: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    confidence: float = 0.0
    depends_on: list[int] = field(default_factory=list)
    condition: str | None = None
    param_bindings: dict[str, str] = field(default_factory=dict)
    source: str = "brain"


@dataclass(slots=True)
class ReasoningStageTrace:
    stage: str
    status: str = "success"
    provider: str = ""
    request_payload: dict[str, Any] = field(default_factory=dict)
    raw_output: dict[str, Any] = field(default_factory=dict)
    normalized_output: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(slots=True)
class BrainDecision:
    intent: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    directives: list[ActionDirective] = field(default_factory=list)
    confidence: float = 0.0
    response: str = ""
    clarification_question: str | None = None
    normalized_text: str = ""
    unresolved_segments: list[str] = field(default_factory=list)
    source: str = "brain"
    reasoning_trace: tuple[ReasoningStageTrace, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class PlanPreview:
    directives: list[ActionDirective] = field(default_factory=list)
    confidence: float = 0.0
    clarification_question: str | None = None
    normalized_text: str = ""
    unresolved_segments: list[str] = field(default_factory=list)
    reasoning_trace: tuple[ReasoningStageTrace, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class ReflectionDecision:
    decision: str = "abort"
    directives: list[ActionDirective] = field(default_factory=list)
    confidence: float = 0.0
    message: str = ""
    question: str | None = None
    normalized_text: str = ""
    unresolved_segments: list[str] = field(default_factory=list)
    source: str = "brain"
    reasoning_trace: tuple[ReasoningStageTrace, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class ExecutionStep:
    action: str
    step_id: int = 0
    target: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[int, ...] = field(default_factory=tuple)
    condition: str | None = None
    param_bindings: dict[str, str] = field(default_factory=dict)
    description: str = ""
    confidence: float = 0.0


@dataclass(slots=True)
class ExecutionPlan:
    intent: str
    goal: str = ""
    steps: list[ExecutionStep] = field(default_factory=list)
    fallback_response: str = ""
    clarification_question: str | None = None
    confidence: float = 0.0
    reasoning_trace: tuple[ReasoningStageTrace, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class ConversationTurn:
    role: str
    content: str


@dataclass(slots=True)
class ContextResolution:
    selected: Any | None = None
    alternatives: tuple[Any, ...] = field(default_factory=tuple)
    clarification_question: str | None = None
    confidence: float = 0.0


class ContextSystem:
    """Maintains short-term conversational state without external dependencies."""

    def __init__(self, *, max_turns: int = 12) -> None:
        self._turns: deque[ConversationTurn] = deque(maxlen=max(2, max_turns))

    def remember_user(self, text: str) -> None:
        self._append_turn("user", text)

    def remember_assistant(self, text: str) -> None:
        self._append_turn("assistant", text)

    def conversation(self) -> list[dict[str, str]]:
        return [{"role": turn.role, "content": turn.content} for turn in self._turns]

    def _append_turn(self, role: str, text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            self._turns.append(ConversationTurn(role=role, content=cleaned))


def resolve_context(
    snapshot: Any | None,
    *,
    requested_action: str,
    explicit_target: str = "",
    require_browser: bool = False,
    ambiguity_margin: float = 0.18,
    min_confidence: float = 0.72,
) -> ContextResolution:
    if snapshot is None:
        return ContextResolution()

    recent_contexts = tuple(getattr(snapshot, "recent_contexts", ()) or ())
    if not recent_contexts:
        return ContextResolution()

    candidates: list[tuple[float, Any]] = []
    normalized_action = _normalize(requested_action)
    normalized_target = _normalize(explicit_target)

    for record in recent_contexts:
        if _record_is_expired(record):
            continue
        if require_browser and not _record_is_browser(record):
            continue
        score = _score_record(record, requested_action=normalized_action, explicit_target=normalized_target)
        if score > 0.0:
            candidates.append((score, record))

    if not candidates:
        return ContextResolution()

    unique: list[tuple[float, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for score, record in sorted(candidates, key=lambda item: item[0], reverse=True):
        key = (
            _normalize(_record_primary_app(record)),
            _normalize(str(getattr(record, "action", ""))),
            _normalize(str(getattr(record, "target", ""))),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append((score, record))

    top_score, top_record = unique[0]
    alternatives = tuple(record for _, record in unique[1:3])
    confidence = max(0.0, min(1.0, top_score / 1.8))

    if alternatives:
        second_score = unique[1][0]
        if _should_ask_for_clarification(top_record, unique[1][1], top_score, second_score, ambiguity_margin):
            return ContextResolution(
                selected=None,
                alternatives=(top_record, unique[1][1]),
                clarification_question=_clarification_prompt(normalized_action, top_record, unique[1][1]),
                confidence=confidence,
            )

    if confidence < min_confidence:
        return ContextResolution(
            selected=None,
            alternatives=(top_record, *alternatives),
            clarification_question=_low_confidence_prompt(requested_action, top_record),
            confidence=confidence,
        )

    return ContextResolution(selected=top_record, alternatives=alternatives, confidence=confidence)


def _normalize(text: str) -> str:
    return " ".join(str(text).strip().lower().split())


def _record_is_expired(record: Any) -> bool:
    value = getattr(record, "is_expired", False)
    return bool(value() if callable(value) else value)


def _record_primary_app(record: Any) -> str:
    value = getattr(record, "primary_app", "")
    return str(value() if callable(value) else value).strip()


def _record_is_browser(record: Any) -> bool:
    primary_app = _normalize(_record_primary_app(record))
    browser_apps = {"chrome", "google chrome", "comet", "edge", "microsoft edge", "firefox"}
    if primary_app in browser_apps:
        return True
    flag = getattr(record, "is_browser", False)
    return bool(flag() if callable(flag) else flag)


def _display_name(record: Any) -> str:
    label = _record_primary_app(record) or str(getattr(record, "target", "")) or str(getattr(record, "action", ""))
    return " ".join(part.capitalize() for part in _normalize(label).split(" ") if part)


def _score_record(record: Any, *, requested_action: str, explicit_target: str) -> float:
    age_seconds = float(getattr(record, "age_seconds", 0.0) or 0.0)
    ttl_seconds = float(getattr(record, "ttl_seconds", 180.0) or 180.0)
    recency = max(0.0, 1.0 - min(age_seconds / max(ttl_seconds, 1.0), 1.0))
    score = recency * 0.55

    primary_app = _normalize(_record_primary_app(record))
    normalized_target = _normalize(str(getattr(record, "target", "")))
    action = _normalize(str(getattr(record, "action", "")))

    if requested_action in {"search_web", "open_url"}:
        if _record_is_browser(record):
            score += 0.85
        if action in {"open_app", "search_web", "open_url"}:
            score += 0.2
    elif requested_action in {"open_app", "focus_app", "close_app"}:
        if primary_app:
            score += 0.5
        if action in {"open_app", "focus_app", "close_app"}:
            score += 0.45
    elif action == requested_action:
        score += 0.5

    if explicit_target:
        if explicit_target == primary_app:
            score += 0.55
        elif explicit_target == normalized_target:
            score += 0.45
        elif explicit_target in normalized_target or explicit_target in primary_app:
            score += 0.2

    if action == requested_action:
        score += 0.1
    return score


def _should_ask_for_clarification(
    first: Any,
    second: Any,
    first_score: float,
    second_score: float,
    ambiguity_margin: float,
) -> bool:
    first_app = _normalize(_record_primary_app(first))
    second_app = _normalize(_record_primary_app(second))
    if not first_app or not second_app or first_app == second_app:
        return False
    return (first_score - second_score) <= ambiguity_margin


def _clarification_prompt(action: str, first: Any, second: Any) -> str:
    first_name = _display_name(first)
    second_name = _display_name(second)
    if action == "search_web":
        return f"Do you want me to search in {first_name} or {second_name}?"
    if action == "open_url":
        return f"Do you want me to open that in {first_name} or {second_name}?"
    return f"Do you mean {first_name} or {second_name}?"


def _low_confidence_prompt(action: str, record: Any) -> str:
    label = _display_name(record)
    if action == "search_web":
        return f"I think you want me to search in {label}, but I'm not confident. Should I use {label}?"
    if action == "open_url":
        return f"I think you want me to open that in {label}, but I'm not confident. Should I use {label}?"
    if action in {"open_app", "focus_app", "close_app"}:
        return f"I think you're referring to {label}, but I'm not confident. Should I use {label}?"
    return f"I found related context for {label}, but I'm not confident enough to assume. Should I use it?"
