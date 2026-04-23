from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
from typing import Any

from jarvis.core.safety.permissions import PermissionLevel


@dataclass(slots=True)
class GuardDecision:
    allowed: bool
    reason: str = "Guard checks passed."
    timeout_seconds: float = 15.0
    error: str = ""
    retry_after_seconds: float = 0.0


class ActionRateLimiter:
    def __init__(self, *, safe_limit: int = 25, moderate_limit: int = 15, dangerous_limit: int = 6, window_seconds: float = 60.0) -> None:
        self._safe_limit = max(0, safe_limit)
        self._moderate_limit = max(0, moderate_limit)
        self._dangerous_limit = max(0, dangerous_limit)
        self._window_seconds = max(1.0, window_seconds)
        self._events: deque[tuple[float, PermissionLevel]] = deque()
        self._lock = threading.Lock()

    def consume(self, level: PermissionLevel) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            limit = self._limit_for(level)
            if limit <= 0:
                return False, self._window_seconds
            if self._count_locked(level) >= limit:
                retry_after = self._retry_after_locked(now, level)
                return False, retry_after
            self._events.append((now, level))
        return True, 0.0

    def snapshot(self) -> dict[str, int]:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            return {
                "safe": self._count_locked(PermissionLevel.SAFE),
                "moderate": self._count_locked(PermissionLevel.MODERATE),
                "dangerous": self._count_locked(PermissionLevel.DANGEROUS),
            }

    def _count_locked(self, level: PermissionLevel) -> int:
        if level == PermissionLevel.DANGEROUS:
            return sum(1 for _, item in self._events if item == PermissionLevel.DANGEROUS)
        if level == PermissionLevel.MODERATE:
            return sum(1 for _, item in self._events if item in {PermissionLevel.MODERATE, PermissionLevel.DANGEROUS})
        return len(self._events)

    def _retry_after_locked(self, now: float, level: PermissionLevel) -> float:
        limit = self._limit_for(level)
        relevant = [timestamp for timestamp, item in self._events if self._matches_level(item, level)]
        if len(relevant) < limit:
            return 0.0
        oldest_relevant = relevant[-limit]
        return max(0.0, self._window_seconds - (now - oldest_relevant))

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _limit_for(self, level: PermissionLevel) -> int:
        if level == PermissionLevel.DANGEROUS:
            return self._dangerous_limit
        if level == PermissionLevel.MODERATE:
            return self._moderate_limit
        return self._safe_limit

    @staticmethod
    def _matches_level(item: PermissionLevel, requested: PermissionLevel) -> bool:
        if requested == PermissionLevel.SAFE:
            return True
        if requested == PermissionLevel.MODERATE:
            return item in {PermissionLevel.MODERATE, PermissionLevel.DANGEROUS}
        return item == PermissionLevel.DANGEROUS


class ExecutionGuard:
    """Preflight checks for rate limits, safe system state, time budgets, and emergency stop."""

    _ALWAYS_ALLOWED = frozenset({"emergency_stop"})
    _TIMEOUTS = {
        "close_active_window": 5.0,
        "close_app": 10.0,
        "create_file": 8.0,
        "delete_file": 8.0,
        "emergency_stop": 3.0,
        "focus_app": 10.0,
        "get_clipboard": 5.0,
        "get_time": 3.0,
        "health_check": 3.0,
        "install_app": 300.0,
        "minimize_window": 5.0,
        "open_app": 20.0,
        "open_explorer": 10.0,
        "open_url": 10.0,
        "overwrite_file": 10.0,
        "play_youtube": 15.0,
        "read_file": 8.0,
        "recall_memory": 5.0,
        "remember_fact": 5.0,
        "report_capabilities": 3.0,
        "save_preference": 5.0,
        "search_web": 10.0,
        "search_youtube": 10.0,
        "set_clipboard": 5.0,
        "set_reminder": 5.0,
        "set_volume": 5.0,
        "switch_window": 10.0,
        "system_action": 10.0,
    }

    def __init__(
        self,
        *,
        event_bus=None,
        cancellation_controller=None,
        rate_limiter: ActionRateLimiter | None = None,
        default_timeout_seconds: float = 15.0,
        moderate_timeout_seconds: float = 25.0,
        dangerous_timeout_seconds: float = 90.0,
    ) -> None:
        self._event_bus = event_bus
        self._cancellation_controller = cancellation_controller
        self._rate_limiter = rate_limiter or ActionRateLimiter()
        self._default_timeout_seconds = max(0.1, default_timeout_seconds)
        self._moderate_timeout_seconds = max(self._default_timeout_seconds, moderate_timeout_seconds)
        self._dangerous_timeout_seconds = max(self._moderate_timeout_seconds, dangerous_timeout_seconds)
        self._lock = threading.Lock()
        self._last_emergency_stop: dict[str, Any] = {}

    def preflight(
        self,
        *,
        action_name: str,
        permission_level: PermissionLevel,
        token=None,
    ) -> GuardDecision:
        normalized_action = str(action_name).strip().lower()
        if token is not None and token.is_cancelled():
            return GuardDecision(allowed=False, reason="Request was cancelled before execution.", error="cancelled")

        if normalized_action not in self._ALWAYS_ALLOWED:
            allowed, retry_after = self._rate_limiter.consume(permission_level)
            if not allowed:
                reason = f"Rate limit exceeded for {permission_level.value} actions. Retry in about {int(max(1, retry_after))}s."
                self._publish("safety.guard_blocked", action=normalized_action, reason=reason, retry_after_seconds=retry_after)
                return GuardDecision(
                    allowed=False,
                    reason=reason,
                    error="rate_limited",
                    retry_after_seconds=retry_after,
                )

        return GuardDecision(
            allowed=True,
            timeout_seconds=self.timeout_for(normalized_action, permission_level=permission_level),
        )

    def timeout_for(self, action_name: str, *, permission_level: PermissionLevel) -> float:
        normalized_action = str(action_name).strip().lower()
        if normalized_action in self._TIMEOUTS:
            return self._TIMEOUTS[normalized_action]
        if permission_level == PermissionLevel.DANGEROUS:
            return self._dangerous_timeout_seconds
        if permission_level == PermissionLevel.MODERATE:
            return self._moderate_timeout_seconds
        return self._default_timeout_seconds

    def trigger_emergency_stop(self, *, reason: str = "Emergency stop requested.") -> dict[str, Any]:
        cancelled_requests: tuple[str, ...] = ()
        if self._cancellation_controller is not None and hasattr(self._cancellation_controller, "cancel_all"):
            cancelled_requests = tuple(self._cancellation_controller.cancel_all())
        payload = {
            "reason": reason,
            "cancelled_requests": list(cancelled_requests),
            "cancelled_count": len(cancelled_requests),
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "rate_limit_snapshot": self._rate_limiter.snapshot(),
        }
        with self._lock:
            self._last_emergency_stop = dict(payload)
        self._publish("safety.emergency_stop", **payload)
        return payload

    def status(self) -> dict[str, Any]:
        with self._lock:
            last_stop = dict(self._last_emergency_stop)
        return {
            "rate_limit_snapshot": self._rate_limiter.snapshot(),
            "last_emergency_stop": last_stop,
        }

    def _publish(self, event_name: str, **payload: Any) -> None:
        if self._event_bus is not None:
            self._event_bus.publish(event_name, payload)
