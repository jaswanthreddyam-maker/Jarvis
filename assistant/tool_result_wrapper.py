from __future__ import annotations

from assistant.contracts import ActionResult
from assistant.tools import ToolResult


def wrap_tool_result(
    action_name: str,
    result: ToolResult,
    *,
    target: str = "",
    **extra_data: object,
) -> ActionResult:
    message = _normalize_message(action_name, result, target=target)
    return ActionResult(
        success=result.success,
        message=message,
        error=result.error,
        data={**result.data, **extra_data, "action": action_name},
    )


def _normalize_message(action_name: str, result: ToolResult, *, target: str) -> str:
    if result.success:
        return result.message

    label = _human_label(target)
    if result.status == "not_found" and action_name in {"focus_app", "close_app"}:
        return f"{label} is not running."
    if result.status == "not_found" and action_name == "switch_window":
        return f"I couldn't find a window matching {label}."
    if result.status == "not_found" and action_name in {"read_file", "delete_file", "overwrite_file"}:
        return f"I couldn't find {label}."
    return result.message


def _human_label(target: str) -> str:
    cleaned = target.strip()
    if not cleaned:
        return "that target"
    if cleaned[0].islower():
        return cleaned[0].upper() + cleaned[1:]
    return cleaned
