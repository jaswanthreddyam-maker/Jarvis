from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from assistant.actions.base import ActionContext
from assistant.cancellation_controller import CancelledError
from assistant.contracts import ActionResult


@dataclass(slots=True)
class ActionDefinition:
    name: str
    func: Callable[[dict[str, Any], ActionContext], ActionResult]
    description: str = ""
    required_params: tuple[str, ...] = field(default_factory=tuple)


class ActionRegistry:
    def __init__(self) -> None:
        self.actions: dict[str, ActionDefinition] = {}

    def register(
        self,
        name: str | None = None,
        func: Callable | None = None,
        description: str = "",
        required_params: tuple[str, ...] = (),
    ) -> Callable | None:
        if func is None:
            def decorator(f: Callable) -> Callable:
                actual_name = name or f.__name__
                self.actions[actual_name] = ActionDefinition(
                    name=actual_name,
                    func=f,
                    description=description,
                    required_params=required_params,
                )
                return f
            return decorator

        actual_name = name or func.__name__
        self.actions[actual_name] = ActionDefinition(
            name=actual_name,
            func=func,
            description=description,
            required_params=required_params,
        )
        return None

    def get_action(self, name: str) -> ActionDefinition | None:
        return self.actions.get(name)

    def list_actions(self) -> list[str]:
        return list(self.actions.keys())

    def call(self, name: str, params: dict[str, Any], context: ActionContext) -> ActionResult:
        action_def = self.actions.get(name)
        if not action_def:
            return ActionResult(
                success=False,
                message=f"Action '{name}' not found.",
                error="action_not_found",
            )

        for param in action_def.required_params:
            if param not in params:
                return ActionResult(
                    success=False,
                    message=f"Missing required parameter '{param}' for action '{name}'.",
                    error="missing_parameter",
                )

        try:
            return action_def.func(params, context)
        except CancelledError:
            raise
        except Exception as e:
            return ActionResult(
                success=False,
                message=f"Error executing action '{name}': {e}",
                error="execution_error",
            )

    # Backwards compatibility for old actions/engine.py
    def get_tool(self, name: str) -> Callable | None:
        action_def = self.get_action(name)
        return action_def.func if action_def else None

registry = ActionRegistry()
