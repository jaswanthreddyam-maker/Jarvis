from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ToolResult:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    status: str = ""

    def __post_init__(self) -> None:
        if not self.status:
            self.status = self._derive_status(self.success, self.error)
        self.data.setdefault("status", self.status)

    @staticmethod
    def _derive_status(success: bool, error: str | None) -> str:
        if success:
            return "completed"
        if (error or "").strip().lower() in {"cancelled", "action_timeout"}:
            return "cancelled"
        return "failed"


@dataclass(slots=True)
class ToolContext:
    request_id: str
    step_id: int
    goal: str
    settings: Any
    project_root: Path
    memory: Any
    scheduler: Any | None = None
    health_service: Any | None = None
    safety_guard: Any | None = None
    cancellation_token: Any | None = None
    event_bus: Any | None = None
    runtime_state: Any | None = None

    def raise_if_cancelled(self, message: str = "Request cancelled.") -> None:
        token = self.cancellation_token
        if token is not None and hasattr(token, "raise_if_cancelled"):
            token.raise_if_cancelled(message)

