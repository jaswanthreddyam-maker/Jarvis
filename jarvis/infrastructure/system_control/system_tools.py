from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path

from jarvis.core.tools import ToolResult


if hasattr(ctypes, "windll"):
    _USER32 = ctypes.windll.user32
    _KERNEL32 = ctypes.windll.kernel32
else:
    _USER32 = None
    _KERNEL32 = None

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF
_KEYEVENTF_KEYUP = 0x0002


def _ensure_windows() -> ToolResult | None:
    if _USER32 is None or _KERNEL32 is None:
        return ToolResult(
            success=False,
            message="System control is only available on Windows.",
            error="unsupported_platform",
        )
    return None


def _press_virtual_key(virtual_key: int, presses: int = 1) -> None:
    for _ in range(max(1, presses)):
        _USER32.keybd_event(virtual_key, 0, 0, 0)
        _USER32.keybd_event(virtual_key, 0, _KEYEVENTF_KEYUP, 0)


def set_volume(value: str) -> ToolResult:
    unsupported = _ensure_windows()
    if unsupported is not None:
        return unsupported

    normalized = str(value).strip().lower()
    if normalized in {"increase", "up", "raise", "louder"}:
        _press_virtual_key(_VK_VOLUME_UP, presses=5)
        return ToolResult(success=True, message="Increased the system volume.", data={"value": "increase"})
    if normalized in {"decrease", "down", "lower", "quieter"}:
        _press_virtual_key(_VK_VOLUME_DOWN, presses=5)
        return ToolResult(success=True, message="Decreased the system volume.", data={"value": "decrease"})
    if normalized in {"mute", "silence"}:
        _press_virtual_key(_VK_VOLUME_MUTE, presses=1)
        return ToolResult(success=True, message="Toggled mute.", data={"value": "mute"})
    return ToolResult(success=False, message=f"Unsupported volume command: {value}", error="invalid_volume_command")


def _known_folder_path(path: str | None, project_root: Path) -> Path:
    home = Path.home()
    aliases = {
        "desktop": home / "Desktop",
        "documents": home / "Documents",
        "downloads": home / "Downloads",
        "music": home / "Music",
        "pictures": home / "Pictures",
        "videos": home / "Videos",
        "home": home,
        "project": project_root.resolve(),
    }
    normalized = str(path or "project").strip().lower()
    return aliases.get(normalized, Path(path or project_root))


def open_explorer(path: str | None, project_root: Path) -> ToolResult:
    target = _known_folder_path(path, project_root)
    resolved = target.resolve(strict=False)
    subprocess.Popen(["explorer", str(resolved)], shell=False)
    return ToolResult(success=True, message=f"Opened {resolved}.", data={"path": str(resolved)})


def get_clipboard() -> ToolResult:
    unsupported = _ensure_windows()
    if unsupported is not None:
        return unsupported

    if not _USER32.OpenClipboard(None):
        return ToolResult(success=False, message="Could not open the clipboard.", error="clipboard_unavailable")

    try:
        handle = _USER32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ToolResult(success=True, message="Clipboard is empty.", data={"text": ""})
        pointer = _KERNEL32.GlobalLock(handle)
        if not pointer:
            return ToolResult(success=False, message="Could not read the clipboard.", error="clipboard_read_failed")
        try:
            text = ctypes.wstring_at(pointer)
        finally:
            _KERNEL32.GlobalUnlock(handle)
    finally:
        _USER32.CloseClipboard()

    return ToolResult(success=True, message="Read the clipboard.", data={"text": text})


def set_clipboard(text: str) -> ToolResult:
    unsupported = _ensure_windows()
    if unsupported is not None:
        return unsupported

    if not _USER32.OpenClipboard(None):
        return ToolResult(success=False, message="Could not open the clipboard.", error="clipboard_unavailable")

    try:
        if not _USER32.EmptyClipboard():
            return ToolResult(success=False, message="Could not clear the clipboard.", error="clipboard_clear_failed")

        payload = str(text).encode("utf-16-le") + b"\x00\x00"
        handle = _KERNEL32.GlobalAlloc(_GMEM_MOVEABLE, len(payload))
        if not handle:
            return ToolResult(success=False, message="Could not allocate clipboard memory.", error="clipboard_alloc_failed")

        pointer = _KERNEL32.GlobalLock(handle)
        if not pointer:
            _KERNEL32.GlobalFree(handle)
            return ToolResult(success=False, message="Could not lock clipboard memory.", error="clipboard_lock_failed")

        try:
            ctypes.memmove(pointer, payload, len(payload))
        finally:
            _KERNEL32.GlobalUnlock(handle)

        if not _USER32.SetClipboardData(_CF_UNICODETEXT, handle):
            _KERNEL32.GlobalFree(handle)
            return ToolResult(success=False, message="Could not set the clipboard.", error="clipboard_set_failed")
    finally:
        _USER32.CloseClipboard()

    return ToolResult(success=True, message="Updated the clipboard.", data={"text": text})

