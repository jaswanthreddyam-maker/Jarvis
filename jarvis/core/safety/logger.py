from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
from typing import Any


logger = logging.getLogger("Jarvis.SafetyAudit")


class SafetyAuditLogger:
    """Append-only JSONL audit trail for user input, planning, permissions, and execution."""

    _EVENTS = (
        "action.completed",
        "action.failed",
        "action.started",
        "execution.cancel_requested",
        "execution.plan_created",
        "permission.blocked",
        "permission.confirm",
        "safety.emergency_stop",
        "safety.guard_blocked",
        "safety.status",
        "safety.timeout",
        "safety.validation",
        "security.response_sent",
        "security.user_input",
    )

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def attach(self, event_bus) -> None:
        for event_name in self._EVENTS:
            event_bus.subscribe(event_name, self._make_handler(event_name))

    def record(self, event_name: str, payload: Any) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_name,
            "payload": self._safe_value(payload),
        }
        line = json.dumps(entry, ensure_ascii=True)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def _make_handler(self, event_name: str):
        def _handler(*args: Any, **kwargs: Any) -> None:
            payload = self._normalize_payload(args, kwargs)
            try:
                self.record(event_name, payload)
            except Exception as exc:
                logger.warning("Failed to append safety audit event '%s': %s", event_name, exc)

        return _handler

    @staticmethod
    def _normalize_payload(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
            return args[0]
        if not args:
            return kwargs
        return {
            "args": list(args),
            "kwargs": kwargs,
        }

    @classmethod
    def _safe_value(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value if len(value) <= 800 else value[:797] + "..."
        if isinstance(value, dict):
            return {str(key): cls._safe_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._safe_value(item) for item in value]
        if hasattr(value, "__dict__"):
            return cls._safe_value(vars(value))
        return repr(value)
