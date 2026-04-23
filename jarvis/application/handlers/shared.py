from __future__ import annotations

from jarvis.core.tools import ToolResult


def truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def should_simulate(params: dict[str, object], context) -> bool:
    return (
        bool(getattr(getattr(context, "settings", None), "simulate_actions", False))
        or truthy(params.get("simulate", False))
        or truthy(params.get("dry_run", False))
    )


def simulated_result(action: str, target: str, **data: object) -> ToolResult:
    return ToolResult(
        success=True,
        message=f"Prepared {action} for {target}.",
        data={**data, "simulated": True, "execution_state": "simulated"},
    )


def tool_result(action_name: str, result: ToolResult, *, target: str = "", **extra_data: object) -> ToolResult:
    merged = dict(result.data)
    merged.update(
        {
            "action": action_name,
            "target": target,
            "simulated": False,
            "execution_state": "executed",
            **extra_data,
        }
    )
    return ToolResult(
        success=result.success,
        message=result.message,
        error=result.error,
        data=merged,
        status=result.status,
    )


def raise_if_cancelled(context, message: str) -> None:
    token = getattr(context, "cancellation_token", None)
    if token is not None:
        token.raise_if_cancelled(message)
