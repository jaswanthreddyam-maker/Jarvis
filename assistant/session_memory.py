from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from assistant.context_manager import ContextManager, ContextSnapshot
from assistant.contracts import TaskPlan


@dataclass(slots=True)
class SessionSnapshot(ContextSnapshot):
    pass


class SessionMemory:
    """Keeps lightweight short-term command context for the current session."""

    def __init__(self, *, ttl_seconds: float = 180.0) -> None:
        self._context = ContextManager(ttl_seconds=ttl_seconds)

    def snapshot(self) -> SessionSnapshot:
        snapshot = self._context.snapshot()
        return SessionSnapshot(
            last_command=snapshot.last_command,
            last_target=snapshot.last_target,
            last_app=snapshot.last_app,
            last_goal=snapshot.last_goal,
            last_params=dict(snapshot.last_params),
            recent_contexts=tuple(snapshot.recent_contexts),
        )

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
        self._context.remember_action(
            user_input=user_input,
            action=action,
            target=resolved_target,
            params=resolved_params,
        )

    def remember_plan(self, user_input: str, plan: TaskPlan) -> None:
        if not plan.steps:
            return

        inherited_app = ""
        for step in plan.steps:
            params = dict(step.params)
            candidate = str(params.get("app_name", "") or params.get("browser_app", "")).strip()
            if candidate:
                inherited_app = candidate
            elif inherited_app and step.action in {"search_web", "open_url"}:
                params.setdefault("browser_app", inherited_app)
            elif inherited_app and step.action in {"focus_app", "close_app"}:
                params.setdefault("app_name", inherited_app)

            self.remember_action(
                user_input=user_input,
                action=step.action,
                target=step.target,
                params=params,
            )

    def clear(self) -> None:
        self._context.clear()
