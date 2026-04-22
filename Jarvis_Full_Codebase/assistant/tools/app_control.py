from __future__ import annotations

import os
import subprocess
from pathlib import Path

from assistant.tools import ToolResult
from assistant.tools.window_control import _find_window, close_window, focus_window


APP_ALIASES: dict[str, dict[str, str]] = {
    "chrome": {"command": "chrome", "process": "chrome.exe", "window": "chrome"},
    "google chrome": {"command": "chrome", "process": "chrome.exe", "window": "chrome"},
    "comet": {"command": "chrome", "process": "chrome.exe", "window": "comet"},
    "edge": {"command": "msedge", "process": "msedge.exe", "window": "edge"},
    "microsoft edge": {"command": "msedge", "process": "msedge.exe", "window": "edge"},
    "firefox": {"command": "firefox", "process": "firefox.exe", "window": "firefox"},
    "notepad": {"command": "notepad", "process": "notepad.exe", "window": "notepad"},
    "terminal": {"command": "wt", "process": "WindowsTerminal.exe", "window": "terminal"},
    "windows terminal": {"command": "wt", "process": "WindowsTerminal.exe", "window": "terminal"},
    "cmd": {"command": "cmd", "process": "cmd.exe", "window": "command prompt"},
    "command prompt": {"command": "cmd", "process": "cmd.exe", "window": "command prompt"},
    "powershell": {"command": "powershell", "process": "powershell.exe", "window": "powershell"},
    "calculator": {"command": "calc", "process": "CalculatorApp.exe", "window": "calculator"},
    "paint": {"command": "mspaint", "process": "mspaint.exe", "window": "paint"},
    "explorer": {"command": "explorer", "process": "explorer.exe", "window": "file explorer"},
    "file explorer": {"command": "explorer", "process": "explorer.exe", "window": "file explorer"},
}


def _normalize_app_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _resolve_alias(name: str) -> dict[str, str]:
    normalized = _normalize_app_name(name)
    return APP_ALIASES.get(normalized, {"command": name.strip(), "process": "", "window": normalized})


def is_app_running(name: str) -> bool:
    alias = _resolve_alias(name)
    process_name = alias.get("process", "").strip()
    window_name = alias.get("window", "").strip()

    if window_name and _find_window(window_name) is not None:
        return True

    if not process_name:
        return False

    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return False

    output = f"{completed.stdout}\n{completed.stderr}".lower()
    return completed.returncode == 0 and process_name.lower() in output


def open_app(name: str) -> ToolResult:
    alias = _resolve_alias(name)
    command = alias["command"]
    if not command:
        return ToolResult(success=False, message="No application name was provided.", error="missing_app_name")

    try:
        candidate_path = Path(command)
        if candidate_path.is_absolute() or candidate_path.exists():
            os.startfile(str(candidate_path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["cmd", "/c", "start", "", command], shell=False)
    except OSError:
        return ToolResult(
            success=False,
            message=f"Could not launch {name.strip()}.",
            error="app_not_found",
            data={"app_name": name.strip(), "command": command},
        )

    return ToolResult(
        success=True,
        message=f"Launched {name.strip()}.",
        data={"app_name": name.strip(), "command": command},
    )


def focus_app(name: str) -> ToolResult:
    alias = _resolve_alias(name)
    return focus_window(alias.get("window") or name.strip())


def close_app(name: str) -> ToolResult:
    alias = _resolve_alias(name)
    close_result = close_window(alias.get("window") or name.strip())
    if close_result.success:
        close_result.data.setdefault("app_name", name.strip())
        return close_result

    process_name = alias.get("process", "").strip()
    if process_name:
        completed = subprocess.run(
            ["taskkill", "/IM", process_name, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            output = (completed.stdout or completed.stderr or "").strip()
            return ToolResult(
                success=True,
                message=f"Closed {name.strip()}.",
                data={"app_name": name.strip(), "process": process_name, "output": output},
            )

    return ToolResult(
        success=False,
        message=f"Could not close {name.strip()}.",
        error=close_result.error or "app_not_found",
    )
