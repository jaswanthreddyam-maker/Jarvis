from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from assistant.settings import load_structured_config


PERMISSION_REASONS = {
    "allow": "Allowed by policy.",
    "sandbox_only": "Reserved for a future sandboxed executor.",
    "confirm_required": "Requires explicit user confirmation.",
    "deny": "Blocked by policy.",
}

SAFE_ACTIONS = frozenset({
    "create_file",
    "focus_app",
    "get_clipboard",
    "open_explorer",
    "read_file",
    "set_clipboard",
    "set_volume",
    "switch_window",
    "open_url",
    "open_app",
    "search_web",
    "web_search",
})

RESTRICTED_ACTIONS = frozenset({
    "delete_file",
    "execute_desktop_workflow",
    "file_op",
    "install_app",
    "overwrite_file",
    "registry_change",
    "registry_changes",
    "run_code",
    "run_command",
    "shell_exec",
    "shell_execution",
    "system_action",
    "system_shutdown",
})


@dataclass(slots=True)
class PermissionDecision:
    status: str          # allow | deny | confirm_required | sandbox_only
    reason: str
    needs_confirmation: bool = False
    action_category: str = "standard"
    safety_level: str = "SAFE"


class PermissionManager:
    """
    Gates actions through a layered policy:
      1. Safe mode blocks restricted actions
      2. YAML config per-action overrides
      3. Restricted/high-risk actions require confirmation
      4. Everything else is allowed
    """

    def __init__(self, path: Path) -> None:
        config = load_structured_config(path)
        self._default: str = config.get("default", "allow")
        self._actions: dict[str, str] = config.get("actions", {})
        self._confirm_callback: Callable[[str, str], bool | None] | None = None

    def set_confirmation_callback(self, callback: Callable[[str, str], bool | None]) -> None:
        """
        Register a callback that is invoked when a risky action needs
        user confirmation. Signature: callback(action_name, description) -> bool
        """
        self._confirm_callback = callback

    @staticmethod
    def action_category_for(action_name: str) -> str:
        if action_name in SAFE_ACTIONS:
            return "safe"
        if action_name in RESTRICTED_ACTIONS:
            return "restricted"
        return "standard"

    def decision_for(
        self,
        action_name: str,
        risk_level: str = "safe",
        description: str = "",
        safe_mode: bool = False,
    ) -> PermissionDecision:
        """
        Determine whether *action_name* is allowed to execute.

        Priority (highest -> lowest):
          1. Safe mode blocks restricted actions.
          2. Explicit YAML deny/sandbox_only always blocks.
          3. YAML confirm_required requires confirmation.
          4. Restricted/high-risk actions require confirmation.
          5. Everything else is allowed.
        """
        yaml_policy = self._actions.get(action_name, self._default)
        action_category = self.action_category_for(action_name)

        if safe_mode and action_category == "restricted":
            return PermissionDecision(
                status="deny",
                reason=f"Safe mode blocks restricted action '{action_name}'.",
                action_category=action_category,
                safety_level="BLOCKED",
            )

        if yaml_policy in {"deny", "sandbox_only"}:
            return PermissionDecision(
                status=yaml_policy,
                reason=PERMISSION_REASONS.get(yaml_policy, "Unknown policy state."),
                action_category=action_category,
                safety_level="BLOCKED",
            )

        needs_confirm = (
            yaml_policy == "confirm_required"
            or action_category == "restricted"
            or risk_level in {"high", "critical"}
        )

        if needs_confirm:
            if self._confirm_callback is not None:
                approved = self._confirm_callback(action_name, description)
                if approved is False:
                    return PermissionDecision(
                        status="deny",
                        reason="User declined the confirmation prompt.",
                        needs_confirmation=True,
                        action_category=action_category,
                        safety_level="BLOCKED",
                    )
                if approved is None:
                    return PermissionDecision(
                        status="confirm_required",
                        reason=f"Action '{action_name}' requires user confirmation.",
                        needs_confirmation=True,
                        action_category=action_category,
                        safety_level="CONFIRMATION REQUIRED",
                    )
            else:
                return PermissionDecision(
                    status="confirm_required",
                    reason=(
                        f"Action '{action_name}' requires user confirmation but "
                        "no confirmation handler is registered."
                    ),
                    needs_confirmation=True,
                    action_category=action_category,
                    safety_level="CONFIRMATION REQUIRED",
                )

        return PermissionDecision(
            status="allow",
            reason=PERMISSION_REASONS["allow"],
            needs_confirmation=needs_confirm,
            action_category=action_category,
            safety_level="SAFE",
        )
