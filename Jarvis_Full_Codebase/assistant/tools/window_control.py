from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

from assistant.tools import ToolResult


if hasattr(ctypes, "windll"):
    _USER32 = ctypes.windll.user32
else:
    _USER32 = None

_SW_MINIMIZE = 6
_SW_RESTORE = 9
_WM_CLOSE = 0x0010


@dataclass(slots=True)
class WindowMatch:
    handle: int
    title: str


def _ensure_windows() -> ToolResult | None:
    if _USER32 is None:
        return ToolResult(
            success=False,
            message="Window control is only available on Windows.",
            error="unsupported_platform",
        )
    return None


def _enumerate_windows() -> list[WindowMatch]:
    matches: list[WindowMatch] = []
    if _USER32 is None:
        return matches

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, lparam: int) -> bool:
        del lparam
        if not _USER32.IsWindowVisible(hwnd):
            return True
        length = _USER32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        _USER32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title:
            matches.append(WindowMatch(handle=int(hwnd), title=title))
        return True

    _USER32.EnumWindows(enum_proc(callback), 0)
    return matches


def get_active_window_title() -> str:
    unsupported = _ensure_windows()
    if unsupported is not None:
        return ""

    hwnd = _USER32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = _USER32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _USER32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def _find_window(target: str) -> WindowMatch | None:
    lowered = target.strip().lower()
    if not lowered:
        return None

    exact: WindowMatch | None = None
    partial: WindowMatch | None = None
    for window in _enumerate_windows():
        title = window.title.lower()
        if title == lowered:
            exact = window
            break
        if lowered in title and partial is None:
            partial = window
    return exact or partial


def focus_window(target: str) -> ToolResult:
    unsupported = _ensure_windows()
    if unsupported is not None:
        return unsupported

    match = _find_window(target)
    if match is None:
        return ToolResult(
            success=False,
            message=f"Could not find a window matching '{target}'.",
            error="window_not_found",
        )

    _USER32.ShowWindow(match.handle, _SW_RESTORE)
    _USER32.SetForegroundWindow(match.handle)
    return ToolResult(
        success=True,
        message=f"Focused '{match.title}'.",
        data={"window_title": match.title, "handle": match.handle},
    )


def switch_window(target: str) -> ToolResult:
    return focus_window(target)


def minimize_active_window() -> ToolResult:
    unsupported = _ensure_windows()
    if unsupported is not None:
        return unsupported

    hwnd = _USER32.GetForegroundWindow()
    if not hwnd:
        return ToolResult(success=False, message="No active window to minimize.", error="window_not_found")

    _USER32.ShowWindow(hwnd, _SW_MINIMIZE)
    return ToolResult(success=True, message="Minimized the active window.", data={"handle": int(hwnd)})


def close_active_window() -> ToolResult:
    unsupported = _ensure_windows()
    if unsupported is not None:
        return unsupported

    hwnd = _USER32.GetForegroundWindow()
    if not hwnd:
        return ToolResult(success=False, message="No active window to close.", error="window_not_found")

    _USER32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
    return ToolResult(success=True, message="Sent a close request to the active window.", data={"handle": int(hwnd)})


def close_window(target: str) -> ToolResult:
    unsupported = _ensure_windows()
    if unsupported is not None:
        return unsupported

    match = _find_window(target)
    if match is None:
        return ToolResult(
            success=False,
            message=f"Could not find a window matching '{target}'.",
            error="window_not_found",
        )

    _USER32.PostMessageW(match.handle, _WM_CLOSE, 0, 0)
    return ToolResult(
        success=True,
        message=f"Sent a close request to '{match.title}'.",
        data={"window_title": match.title, "handle": match.handle},
    )
