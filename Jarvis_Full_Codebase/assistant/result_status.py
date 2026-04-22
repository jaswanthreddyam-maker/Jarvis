from __future__ import annotations

NOT_FOUND_ERROR_CODES = frozenset(
    {
        "action_not_found",
        "app_not_found",
        "file_not_found",
        "window_not_found",
    }
)


def derive_status(success: bool, error: str | None) -> str:
    if success:
        return "success"
    if (error or "").strip().lower() in NOT_FOUND_ERROR_CODES:
        return "not_found"
    return "error"
