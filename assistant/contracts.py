from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone, datetime
from enum import Enum
from typing import Any

from assistant.result_status import derive_status


class RiskLevel(str, Enum):
    """Classification for how dangerous an action is."""
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class WaitType(str, Enum):
    """Type of condition-based wait before/after a step."""
    NONE = "none"
    FIXED_DELAY = "fixed_delay"
    WINDOW_TITLE = "window_title"
    URL_CONTAINS = "url_contains"
    ELEMENT_VISIBLE = "element_visible"


@dataclass(slots=True)
class WaitCondition:
    """Describes a wait condition that must be satisfied before continuing."""
    wait_type: WaitType = WaitType.NONE
    value: str = ""
    timeout: float = 10.0
    poll_interval: float = 0.5


@dataclass(slots=True)
class StepDefinition:
    action: str
    step_id: int = 0
    target: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[int, ...] = field(default_factory=tuple)
    param_bindings: dict[str, str] = field(default_factory=dict)
    description: str = ""
    verification: str = ""
    fallback_action: str = ""
    fallback_target: str = ""
    fallback_params: dict[str, Any] = field(default_factory=dict)
    fallback_description: str = ""
    fallback_verification: str = ""
    max_retries: int = 1
    retry_group: str = "default"
    risk_level: RiskLevel = RiskLevel.SAFE
    expected_window: str = ""
    pre_wait: WaitCondition = field(default_factory=WaitCondition)
    post_wait: WaitCondition = field(default_factory=WaitCondition)


@dataclass(slots=True)
class TaskPlan:
    intent: str
    goal: str = ""
    steps: list[StepDefinition] = field(default_factory=list)
    goal_state: dict[str, Any] = field(default_factory=dict)
    fallback_response: str = ""
    clarification_question: str | None = None
    requires_approval: bool = False
    reused_from_memory: bool = False
    overall_risk: RiskLevel = RiskLevel.SAFE
    expected_window: str = ""


@dataclass(slots=True)
class ActionResult:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    status: str = ""

    def __post_init__(self) -> None:
        if not self.status:
            self.status = derive_status(self.success, self.error)
        self.data.setdefault("status", self.status)


@dataclass(slots=True)
class StepState:
    step_id: str
    plan_step_id: int
    action: str
    target: str
    params: dict[str, Any]
    description: str
    depends_on: tuple[int, ...] = field(default_factory=tuple)
    param_bindings: dict[str, str] = field(default_factory=dict)
    verification: str = ""
    fallback_action: str = ""
    fallback_target: str = ""
    fallback_params: dict[str, Any] = field(default_factory=dict)
    fallback_description: str = ""
    fallback_verification: str = ""
    max_retries: int = 1
    retry_group: str = "default"
    risk_level: str = "safe"
    expected_window: str = ""
    status: str = "pending"
    attempts: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    error: str | None = None


@dataclass(slots=True)
class TaskState:
    task_id: str
    user_input: str
    intent: str
    status: str = "pending"
    steps: list[StepState] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    final_response: str = ""


class FeedbackLevel(str, Enum):
    """Severity levels for execution feedback."""
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    BLOCKED = "blocked"


@dataclass(slots=True)
class ExecutionFeedback:
    """A single unit of feedback emitted during execution for UI display."""
    task_id: str
    step_index: int
    step_total: int
    action: str
    description: str
    level: FeedbackLevel
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
