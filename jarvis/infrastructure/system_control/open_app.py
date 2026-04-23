from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path

from jarvis.core.cancellation import CancelledError
from jarvis.core.tools import ToolResult
from jarvis.infrastructure.system_control import app_control, system_tools, window_control


def open_app(app_name: str) -> ToolResult:
    return app_control.open_app(app_name)


def focus_app(app_name: str) -> ToolResult:
    return app_control.focus_app(app_name)


def close_app(app_name: str) -> ToolResult:
    return app_control.close_app(app_name)


def open_explorer(path: str, *, project_root: Path) -> ToolResult:
    return system_tools.open_explorer(path=path, project_root=project_root)


def set_volume(value: str) -> ToolResult:
    return system_tools.set_volume(value)


def get_clipboard() -> ToolResult:
    return system_tools.get_clipboard()


def set_clipboard(text: str) -> ToolResult:
    return system_tools.set_clipboard(text)


def minimize_window() -> ToolResult:
    return window_control.minimize_active_window()


def switch_window(title: str) -> ToolResult:
    return window_control.switch_window(title)


def close_active_window() -> ToolResult:
    return window_control.close_active_window()


def get_time() -> ToolResult:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return ToolResult(success=True, message=f"The local time is {now}.", data={"time": now})


def system_action(action_type: str, *, confirmed: bool) -> ToolResult:
    normalized = action_type.strip().lower()
    if normalized not in {"shutdown", "restart", "reboot"}:
        return ToolResult(success=False, message=f"Unsupported system action: {action_type}", error="unsupported_system_action")
    if not confirmed:
        return ToolResult(
            success=False,
            message="Confirmation required before running a system power action.",
            error="confirmation_required",
            data={"action_type": normalized, "needs_confirmation": True},
        )

    command = ["shutdown", "/r" if normalized in {"restart", "reboot"} else "/s", "/t", "0"]
    try:
        subprocess.Popen(command, shell=False)
    except OSError as exc:
        return ToolResult(
            success=False,
            message=f"Failed to execute system action {normalized}: {exc}",
            error="system_action_failed",
            data={"action_type": normalized},
        )
    return ToolResult(success=True, message=f"Executed system action: {normalized}", data={"action_type": normalized})


def install_app(app_name: str, *, token=None) -> ToolResult:
    completed = _run_winget(
        [
            "install",
            "--exact",
            "--accept-package-agreements",
            "--accept-source-agreements",
            app_name,
        ],
        token=token,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        rollback = _run_winget(["uninstall", "--exact", "--accept-source-agreements", app_name], token=token)
        rollback_output = (rollback.stdout or rollback.stderr or "").strip()
        rollback_succeeded = rollback.returncode == 0
        rollback_summary = (
            f"Rollback succeeded: {rollback_output or 'package uninstall completed.'}"
            if rollback_succeeded
            else f"Rollback attempted but failed: {rollback_output or 'package uninstall could not be completed.'}"
        )
        return ToolResult(
            success=False,
            message=f"Failed to install {app_name}. {output} {rollback_summary}".strip(),
            error="install_failed",
            data={
                "app_name": app_name,
                "returncode": completed.returncode,
                "rollback_attempted": True,
                "rollback_succeeded": rollback_succeeded,
                "rollback_message": rollback_output or rollback_summary,
            },
        )

    return ToolResult(
        success=True,
        message=f"Installed {app_name}.",
        data={"app_name": app_name, "output": output},
    )


def _run_winget(args: list[str], *, token=None) -> subprocess.CompletedProcess[str]:
    command = ["winget", *args]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if token is not None:
            token.track_subprocess(process)
        while True:
            if token is not None and token.is_cancelled():
                raise CancelledError("Cancelled the running system action.")
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(command, process.returncode, stdout or "", stderr or "")
            time.sleep(0.05)
    except OSError as exc:
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr=str(exc))
