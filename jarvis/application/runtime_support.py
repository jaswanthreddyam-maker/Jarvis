from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import time
from typing import Any

from jarvis.config.constants import BROWSER_APPS
from jarvis.core.context import ExecutionPlan


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(text: str) -> str:
    return " ".join(str(text).strip().lower().split())


@dataclass(slots=True)
class SessionActionRecord:
    action: str
    target: str
    params: dict[str, Any]
    goal: str
    created_at: float
    ttl_seconds: float

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
        return _normalize(str(self.params.get("app_name", "")))

    @property
    def is_browser(self) -> bool:
        return self.primary_app in BROWSER_APPS


@dataclass(slots=True)
class SessionContextSnapshot:
    last_command: str = ""
    last_target: str = ""
    last_app: str = ""
    last_goal: str = ""
    last_params: dict[str, Any] = field(default_factory=dict)
    recent_contexts: tuple[SessionActionRecord, ...] = field(default_factory=tuple)

    def as_payload(self) -> dict[str, Any]:
        return {
            "last_command": self.last_command,
            "last_target": self.last_target,
            "last_app": self.last_app,
            "last_goal": self.last_goal,
            "last_params": dict(self.last_params),
            "recent_contexts": tuple(self.recent_contexts),
        }


class SessionContextStore:
    def __init__(self, *, ttl_seconds: float = 180.0, max_items: int = 24) -> None:
        self._ttl_seconds = ttl_seconds
        self._records: deque[SessionActionRecord] = deque(maxlen=max_items)
        self._lock = threading.RLock()

    def remember_action(
        self,
        *,
        user_input: str,
        action: str,
        target: str = "",
        params: dict[str, Any] | None = None,
    ) -> None:
        resolved_params = dict(params or {})
        resolved_target = target.strip() or str(
            resolved_params.get("url")
            or resolved_params.get("query")
            or resolved_params.get("name")
            or resolved_params.get("path")
            or resolved_params.get("app_name")
            or resolved_params.get("title")
            or ""
        ).strip()
        with self._lock:
            self._prune_locked()
            self._records.appendleft(
                SessionActionRecord(
                    action=_normalize(action),
                    target=resolved_target,
                    params=resolved_params,
                    goal=user_input.strip(),
                    created_at=time.monotonic(),
                    ttl_seconds=self._ttl_seconds,
                )
            )

    def remember_plan(self, user_input: str, plan: ExecutionPlan, *, results: list[dict[str, Any]] | None = None) -> None:
        result_map = {int(item.get("step_id", 0)): item for item in list(results or []) if int(item.get("step_id", 0) or 0) > 0}
        for step in plan.steps:
            result = result_map.get(int(step.step_id))
            if result is not None and not bool(result.get("success", False)):
                continue
            params = dict(step.params)
            if result is not None:
                result_data = dict(result.get("result") or {})
                for key in ("app_name", "browser_app", "path", "url", "query", "title"):
                    if key not in params and key in result_data:
                        params[key] = result_data[key]
            self.remember_action(user_input=user_input, action=step.action, target=step.target, params=params)

    def snapshot(self) -> SessionContextSnapshot:
        with self._lock:
            self._prune_locked()
            latest = self._records[0] if self._records else None
            return SessionContextSnapshot(
                last_command=latest.action if latest is not None else "",
                last_target=latest.target if latest is not None else "",
                last_app=latest.primary_app if latest is not None else "",
                last_goal=latest.goal if latest is not None else "",
                last_params=dict(latest.params) if latest is not None else {},
                recent_contexts=tuple(self._records),
            )

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def _prune_locked(self) -> None:
        while self._records and self._records[-1].is_expired:
            self._records.pop()


@dataclass(slots=True)
class Reminder:
    run_at: datetime
    topic: str
    payload: dict[str, Any]


class ReminderScheduler:
    def __init__(self) -> None:
        self._items: list[Reminder] = []
        self._lock = threading.RLock()

    def schedule(self, *, run_at: datetime, topic: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._items.append(Reminder(run_at=run_at, topic=topic, payload=dict(payload)))
            self._items.sort(key=lambda item: item.run_at)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._items)

    def poll_due(self) -> list[dict[str, Any]]:
        now = _now_utc()
        due: list[Reminder] = []
        with self._lock:
            remaining: list[Reminder] = []
            for item in self._items:
                if item.run_at <= now:
                    due.append(item)
                else:
                    remaining.append(item)
            self._items = remaining
        return [dict(item.payload) | {"topic": item.topic} for item in due]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Any]] = {}
        self._lock = threading.RLock()

    def subscribe(self, event_name: str, callback) -> None:
        with self._lock:
            self._subscribers.setdefault(event_name, []).append(callback)

    def unsubscribe(self, event_name: str, callback) -> None:
        with self._lock:
            listeners = self._subscribers.get(event_name, [])
            if callback in listeners:
                listeners.remove(callback)

    def publish(self, event_name: str, payload: Any) -> None:
        with self._lock:
            listeners = list(self._subscribers.get(event_name, []))
        for callback in listeners:
            try:
                callback(payload)
            except Exception:
                continue

    def publish_async(self, event_name: str, payload: Any) -> None:
        thread = threading.Thread(target=self.publish, args=(event_name, payload), daemon=True)
        thread.start()

    def shutdown(self) -> None:
        with self._lock:
            self._subscribers.clear()
