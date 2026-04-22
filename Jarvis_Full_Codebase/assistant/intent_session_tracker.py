from __future__ import annotations

import re
import time
from dataclasses import dataclass

from assistant.contracts import TaskPlan


_FOLLOW_UP_MARKERS = (
    "it",
    "that",
    "again",
    "same",
    "continue",
    "open it",
    "open that",
    "search it",
    "do it again",
)
_FOLLOW_UP_PATTERNS = tuple(
    re.compile(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])")
    for marker in sorted(_FOLLOW_UP_MARKERS, key=len, reverse=True)
)
_COMPATIBLE_FOLLOW_UP_ACTIONS = {
    ("search_youtube", "play_youtube"),
    ("open_url", "play_youtube"),
}


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _normalize(text)))


@dataclass(slots=True)
class IntentSession:
    goal: str = ""
    intent: str = ""
    primary_action: str = ""
    updated_at: float = 0.0


@dataclass(slots=True)
class IntentAssessment:
    drift_score: float = 0.0
    intent_changed: bool = False
    clarification_question: str | None = None
    should_reset: bool = False


class IntentSessionTracker:
    """Tracks the active user intent and flags drift for fragile follow-up requests."""

    def __init__(self, *, ttl_seconds: float = 300.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._session = IntentSession()

    def assess(self, user_input: str, plan: TaskPlan) -> IntentAssessment:
        self._expire_if_needed()
        if not self._session.goal:
            return IntentAssessment()

        normalized = _normalize(user_input)
        active_tokens = _tokenize(self._session.goal)
        current_tokens = _tokenize(plan.goal or user_input)
        overlap = _overlap_score(active_tokens, current_tokens)
        drift_score = 1.0 - overlap
        primary_action = plan.steps[0].action if plan.steps else plan.intent
        follow_up_like = any(pattern.search(normalized) for pattern in _FOLLOW_UP_PATTERNS)
        intent_changed = bool(primary_action and self._session.primary_action and primary_action != self._session.primary_action)
        compatible_follow_up = (self._session.primary_action, primary_action) in _COMPATIBLE_FOLLOW_UP_ACTIONS

        if follow_up_like and drift_score >= 0.85 and intent_changed and not compatible_follow_up:
            label = self._session.primary_action.replace("_", " ") if self._session.primary_action else "the previous task"
            return IntentAssessment(
                drift_score=drift_score,
                intent_changed=True,
                clarification_question=(
                    f"Your request sounds like a follow-up, but the active intent has changed from {label}. "
                    "Should I use the previous task context or treat this as a new request?"
                ),
            )

        return IntentAssessment(
            drift_score=drift_score,
            intent_changed=intent_changed,
            should_reset=drift_score >= 0.6 and not follow_up_like,
        )

    def remember(self, user_input: str, plan: TaskPlan) -> None:
        if not plan.steps:
            return
        self._session = IntentSession(
            goal=(plan.goal or user_input).strip(),
            intent=plan.intent,
            primary_action=plan.steps[0].action,
            updated_at=time.monotonic(),
        )

    def clear(self) -> None:
        self._session = IntentSession()

    def snapshot(self) -> IntentSession:
        self._expire_if_needed()
        return IntentSession(
            goal=self._session.goal,
            intent=self._session.intent,
            primary_action=self._session.primary_action,
            updated_at=self._session.updated_at,
        )

    def _expire_if_needed(self) -> None:
        if not self._session.goal:
            return
        if (time.monotonic() - self._session.updated_at) > self._ttl_seconds:
            self.clear()


def _overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)
