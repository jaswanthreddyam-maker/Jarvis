from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4


# -----------------------------
# Intents
# -----------------------------


class ExecutionIntent:
    """Marker base class for runtime intents."""


@dataclass(slots=True)
class OpenIntent(ExecutionIntent):
    type: str = "open"
    target: str = ""


@dataclass(slots=True)
class SearchIntent(ExecutionIntent):
    type: str = "search"
    query: str = ""


@dataclass(slots=True)
class PlayIntent(ExecutionIntent):
    type: str = "play"
    query: str = ""
    platform: str = "youtube"
    action_type: str | None = None


@dataclass(slots=True)
class NoOpIntent(ExecutionIntent):
    type: str = "no_op"
    message: str = ""


@dataclass(slots=True)
class OrchestratorStepIntent(ExecutionIntent):
    type: str = "orchestrator_execute"
    step_obj: Any = None


def describe_execution_intent(intent: ExecutionIntent | None) -> str:
    if intent is None:
        return "none"
    if isinstance(intent, OpenIntent):
        return f"open:{intent.target}"
    if isinstance(intent, SearchIntent):
        return f"search:{intent.query}"
    if isinstance(intent, PlayIntent):
        return f"play:{intent.platform}:{intent.query}"
    if isinstance(intent, NoOpIntent):
        return f"no_op:{intent.message}"
    if isinstance(intent, OrchestratorStepIntent):
        action = getattr(intent.step_obj, "action", None) or getattr(intent.step_obj, "type", None) or "unknown"
        target = getattr(intent.step_obj, "target", "") or ""
        return f"orchestrator:{action}:{target}"
    return intent.__class__.__name__


# -----------------------------
# Execution steps & state
# -----------------------------


@dataclass(slots=True)
class ExecutionPolicy:
    max_attempts: int = 2
    backoff_seconds: float = 0.2
    fallback_strategy: Literal["continue", "abort"] = "continue"


@dataclass(slots=True)
class ExecutionStep:
    intent: ExecutionIntent
    source: str = "user"
    id: str = field(default_factory=lambda: uuid4().hex)
    dependencies: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    metadata: dict[str, Any] = field(default_factory=dict)


class ExecutionState:
    """Execution runtime state. Structure must remain stable."""

    def __init__(self) -> None:
        self.history: list[ExecutionIntent] = []
        self.completed_steps: list[str] = []
        self.failed_steps: list[str] = []
        self.context: dict[str, Any] = {}


# -----------------------------
# Commands
# -----------------------------


@dataclass(frozen=True, slots=True)
class ExecutionCommand:
    type: Literal["text", "intent"]
    text: str = ""
    intent: ExecutionIntent | None = None
    source_text: str | None = None

    @property
    def display_text(self) -> str:
        if self.type == "text":
            return self.text
        return self.source_text or describe_execution_intent(self.intent)

    @staticmethod
    def for_text(text: str) -> "ExecutionCommand":
        return ExecutionCommand(type="text", text=str(text or ""))

    @staticmethod
    def for_intent(intent: ExecutionIntent, *, source_text: str = "") -> "ExecutionCommand":
        return ExecutionCommand(type="intent", intent=intent, source_text=str(source_text or ""))

