from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from assistant.result_status import derive_status


@dataclass(slots=True)
class ToolResult:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    status: str = ""

    def __post_init__(self) -> None:
        if not self.status:
            self.status = derive_status(self.success, self.error)
        self.data.setdefault("status", self.status)


__all__ = ["ToolResult"]
