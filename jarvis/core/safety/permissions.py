from __future__ import annotations

from enum import Enum


class PermissionLevel(str, Enum):
    SAFE = "safe"
    MODERATE = "moderate"
    DANGEROUS = "dangerous"


_ACTION_PERMISSION_LEVELS: dict[str, PermissionLevel] = {
    "close_active_window": PermissionLevel.MODERATE,
    "close_app": PermissionLevel.MODERATE,
    "create_file": PermissionLevel.MODERATE,
    "delete_file": PermissionLevel.DANGEROUS,
    "delete_folder": PermissionLevel.DANGEROUS,
    "emergency_stop": PermissionLevel.SAFE,
    "execute_desktop_workflow": PermissionLevel.DANGEROUS,
    "file_op": PermissionLevel.DANGEROUS,
    "focus_app": PermissionLevel.SAFE,
    "get_clipboard": PermissionLevel.SAFE,
    "get_time": PermissionLevel.SAFE,
    "health_check": PermissionLevel.SAFE,
    "install_app": PermissionLevel.DANGEROUS,
    "minimize_window": PermissionLevel.SAFE,
    "open_app": PermissionLevel.SAFE,
    "open_explorer": PermissionLevel.SAFE,
    "open_url": PermissionLevel.SAFE,
    "overwrite_file": PermissionLevel.MODERATE,
    "play_youtube": PermissionLevel.SAFE,
    "read_file": PermissionLevel.SAFE,
    "recall_memory": PermissionLevel.SAFE,
    "remember_fact": PermissionLevel.MODERATE,
    "remove_folder": PermissionLevel.DANGEROUS,
    "report_capabilities": PermissionLevel.SAFE,
    "save_preference": PermissionLevel.MODERATE,
    "run_code": PermissionLevel.DANGEROUS,
    "run_command": PermissionLevel.DANGEROUS,
    "search_web": PermissionLevel.SAFE,
    "search_youtube": PermissionLevel.SAFE,
    "set_clipboard": PermissionLevel.MODERATE,
    "set_reminder": PermissionLevel.MODERATE,
    "set_volume": PermissionLevel.SAFE,
    "shell_exec": PermissionLevel.DANGEROUS,
    "shell_execution": PermissionLevel.DANGEROUS,
    "switch_window": PermissionLevel.SAFE,
    "system_action": PermissionLevel.DANGEROUS,
}


def permission_level_for(action_name: str, risk_level: str = "safe") -> PermissionLevel:
    level = _ACTION_PERMISSION_LEVELS.get(str(action_name).strip().lower(), PermissionLevel.SAFE)
    risk = str(risk_level).strip().lower()
    if risk in {"critical", "high"}:
        return _max_level(level, PermissionLevel.DANGEROUS)
    if risk in {"medium"}:
        return _max_level(level, PermissionLevel.MODERATE)
    return level


def _max_level(left: PermissionLevel, right: PermissionLevel) -> PermissionLevel:
    order = {
        PermissionLevel.SAFE: 0,
        PermissionLevel.MODERATE: 1,
        PermissionLevel.DANGEROUS: 2,
    }
    return left if order[left] >= order[right] else right
