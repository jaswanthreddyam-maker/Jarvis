from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


_BROWSER_APPS = frozenset({"chrome", "google chrome", "comet", "edge", "microsoft edge", "firefox"})
_YOUTUBE_HINTS = ("youtube.com", "youtu.be")


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


@dataclass(slots=True)
class ContextRecord:
    action: str
    target: str
    params: dict[str, Any]
    goal: str
    created_at: float
    ttl_seconds: float
    app_name: str = ""

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.created_at)

    @property
    def is_expired(self) -> bool:
        return self.age_seconds > self.ttl_seconds

    @property
    def primary_app(self) -> str:
        browser_app = _normalize(str(self.params.get("browser_app", "")))
        if browser_app:
            return browser_app
        if self.app_name:
            return _normalize(self.app_name)
        return _normalize(str(self.params.get("app_name", "")))

    @property
    def is_browser(self) -> bool:
        return self.primary_app in _BROWSER_APPS


@dataclass(slots=True)
class ContextSnapshot:
    last_command: str = ""
    last_target: str = ""
    last_app: str = ""
    last_goal: str = ""
    last_params: dict[str, Any] = field(default_factory=dict)
    recent_contexts: tuple[ContextRecord, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class ContextResolution:
    selected: ContextRecord | None = None
    alternatives: tuple[ContextRecord, ...] = field(default_factory=tuple)
    clarification_question: str | None = None
    confidence: float = 0.0


class ContextManager:
    """Maintains short-lived high-signal context with recency and relevance scoring."""

    def __init__(self, *, ttl_seconds: float = 180.0, max_items: int = 16) -> None:
        self._ttl_seconds = ttl_seconds
        self._records: deque[ContextRecord] = deque(maxlen=max_items)

    def remember_action(
        self,
        *,
        user_input: str,
        action: str,
        target: str = "",
        params: dict[str, Any] | None = None,
    ) -> None:
        resolved_params = dict(params or {})
        app_name = str(
            resolved_params.get("app_name")
            or resolved_params.get("title")
            or resolved_params.get("browser_app")
            or resolved_params.get("active_app")
            or ""
        ).strip()
        if not app_name:
            app_name = _infer_active_app(action=action, target=target, params=resolved_params)
        self._prune()
        self._records.appendleft(
            ContextRecord(
                action=_normalize(action),
                target=target.strip(),
                params=resolved_params,
                goal=user_input.strip(),
                created_at=time.monotonic(),
                ttl_seconds=self._ttl_seconds,
                app_name=app_name,
            )
        )

    def snapshot(self) -> ContextSnapshot:
        self._prune()
        latest = self._records[0] if self._records else None
        return ContextSnapshot(
            last_command=latest.action if latest is not None else "",
            last_target=latest.target if latest is not None else "",
            last_app=latest.primary_app if latest is not None else "",
            last_goal=latest.goal if latest is not None else "",
            last_params=dict(latest.params) if latest is not None else {},
            recent_contexts=tuple(self._records),
        )

    def clear(self) -> None:
        self._records.clear()

    def _prune(self) -> None:
        while self._records and self._records[-1].is_expired:
            self._records.pop()


def resolve_context(
    snapshot: ContextSnapshot | None,
    *,
    requested_action: str,
    explicit_target: str = "",
    require_browser: bool = False,
    ambiguity_margin: float = 0.18,
    min_confidence: float = 0.72,
) -> ContextResolution:
    if snapshot is None:
        return ContextResolution()

    candidates: list[tuple[float, ContextRecord]] = []
    normalized_action = _normalize(requested_action)
    normalized_target = _normalize(explicit_target)

    for record in snapshot.recent_contexts:
        if record.is_expired:
            continue
        if require_browser and not record.is_browser:
            continue

        score = _score_record(record, requested_action=normalized_action, explicit_target=normalized_target)
        if score <= 0.0:
            continue
        candidates.append((score, record))

    if not candidates:
        return ContextResolution()

    unique: list[tuple[float, ContextRecord]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for score, record in sorted(candidates, key=lambda item: item[0], reverse=True):
        key = (_normalize(record.primary_app), _normalize(record.action), _normalize(record.target))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append((score, record))

    top_score, top_record = unique[0]
    alternatives = tuple(record for _, record in unique[1:3])

    if alternatives:
        second_score = unique[1][0]
        if _should_ask_for_clarification(top_record, unique[1][1], top_score, second_score, ambiguity_margin):
            question = _clarification_prompt(normalized_action, top_record, unique[1][1])
            return ContextResolution(
                selected=None,
                alternatives=(top_record, unique[1][1]),
                clarification_question=question,
                confidence=max(0.0, min(1.0, top_score / 1.8)),
            )

    confidence = max(0.0, min(1.0, top_score / 1.8))
    if confidence < min_confidence:
        return ContextResolution(
            selected=None,
            alternatives=(top_record, *alternatives),
            clarification_question=_low_confidence_prompt(requested_action, top_record),
            confidence=confidence,
        )

    return ContextResolution(selected=top_record, alternatives=alternatives, confidence=confidence)


def _score_record(record: ContextRecord, *, requested_action: str, explicit_target: str) -> float:
    recency = max(0.0, 1.0 - min(record.age_seconds / max(record.ttl_seconds, 1.0), 1.0))
    score = recency * 0.55

    primary_app = _normalize(record.primary_app)
    normalized_target = _normalize(record.target)

    if requested_action in {"search_web", "open_url"}:
        if record.is_browser:
            score += 0.85
        if record.action in {"open_app", "search_web", "open_url"}:
            score += 0.2
    elif requested_action in {"open_app", "focus_app", "close_app"}:
        if primary_app:
            score += 0.5
        if record.action in {"open_app", "focus_app", "close_app"}:
            score += 0.45
    elif record.action == requested_action:
        score += 0.5

    if explicit_target:
        if explicit_target == primary_app:
            score += 0.55
        elif explicit_target == normalized_target:
            score += 0.45
        elif explicit_target in normalized_target or explicit_target in primary_app:
            score += 0.2

    if record.action == requested_action:
        score += 0.1
    return score


def _should_ask_for_clarification(
    first: ContextRecord,
    second: ContextRecord,
    first_score: float,
    second_score: float,
    ambiguity_margin: float,
) -> bool:
    first_app = _normalize(first.primary_app)
    second_app = _normalize(second.primary_app)
    if not first_app or not second_app or first_app == second_app:
        return False
    return (first_score - second_score) <= ambiguity_margin


def _clarification_prompt(action: str, first: ContextRecord, second: ContextRecord) -> str:
    first_name = _display_name(first)
    second_name = _display_name(second)
    if action == "search_web":
        return f"Do you want me to search in {first_name} or {second_name}?"
    if action == "open_url":
        return f"Do you want me to open that in {first_name} or {second_name}?"
    return f"Do you mean {first_name} or {second_name}?"


def _low_confidence_prompt(action: str, record: ContextRecord) -> str:
    label = _display_name(record)
    if action == "search_web":
        return f"I think you want me to search in {label}, but I'm not confident. Should I use {label}?"
    if action == "open_url":
        return f"I think you want me to open that in {label}, but I'm not confident. Should I use {label}?"
    if action in {"open_app", "focus_app", "close_app"}:
        return f"I think you're referring to {label}, but I'm not confident. Should I use {label}?"
    return f"I found related context for {label}, but I'm not confident enough to assume. Should I use it?"


def _display_name(record: ContextRecord) -> str:
    label = record.primary_app or record.target or record.action
    return " ".join(part.capitalize() for part in _normalize(label).split(" "))


def _infer_active_app(*, action: str, target: str, params: dict[str, Any]) -> str:
    normalized_action = _normalize(action)
    normalized_target = _normalize(target)
    url = str(params.get("url") or params.get("search_url") or "").strip().lower()

    if normalized_action == "search_youtube":
        return "youtube"
    if normalized_action == "open_url":
        if normalized_target == "youtube" or any(hint in url for hint in _YOUTUBE_HINTS):
            return "youtube"
        return normalized_target
    return ""
