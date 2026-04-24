from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Interaction:
    user: str
    assistant: str
    timestamp: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "assistant": self.assistant,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


class ShortTermMemory:
    """Small in-memory buffer for recent interactions within the active session."""

    def __init__(self, *, max_items: int = 8) -> None:
        self._items: deque[Interaction] = deque(maxlen=max(1, max_items))
        self._lock = threading.RLock()

    def add_interaction(
        self,
        user: str,
        assistant: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Interaction:
        interaction = Interaction(
            user=user.strip(),
            assistant=assistant.strip(),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._items.append(interaction)
        return interaction

    def recent(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items)
        if limit is not None and limit >= 0:
            items = items[-limit:]
        return [item.as_dict() for item in items]

    def last_interaction(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._items:
                return None
            return self._items[-1].as_dict()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
