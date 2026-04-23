from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jarvis.config.constants import BROWSER_APPS, KNOWN_FOLDERS
from jarvis.infrastructure.system_control.file_ops import resolve_safe_path
from jarvis.infrastructure.web.browser import normalize_url


_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}


@dataclass(slots=True)
class SandboxDecision:
    allowed: bool
    reason: str = "Sandbox policy validated."
    sanitized_params: dict[str, Any] = field(default_factory=dict)
    sandbox_mode: str = "policy"
    allowed_roots: tuple[str, ...] = field(default_factory=tuple)


class ExecutionSandbox:
    """Restricts execution to approved files, schemes, apps, and hosts."""

    _FILE_ACTIONS = frozenset({"create_file", "overwrite_file", "read_file", "delete_file"})
    _URL_ACTIONS = frozenset({"open_url", "play_youtube"})
    _SEARCH_ACTIONS = frozenset({"search_web", "search_youtube"})

    def evaluate(
        self,
        *,
        action_name: str,
        params: dict[str, Any],
        project_root: Path,
    ) -> SandboxDecision:
        normalized_action = str(action_name).strip().lower()
        sanitized = dict(params)

        browser_app = str(sanitized.get("browser_app", "") or "").strip().lower()
        if browser_app and browser_app not in BROWSER_APPS:
            return SandboxDecision(allowed=False, reason=f"Browser '{browser_app}' is not allowed.")

        if normalized_action in self._FILE_ACTIONS:
            raw_name = str(sanitized.get("name", "") or "").strip()
            try:
                resolved = resolve_safe_path(raw_name, project_root)
            except ValueError as exc:
                return SandboxDecision(allowed=False, reason=str(exc))
            sanitized["_resolved_path"] = str(resolved)
            return SandboxDecision(
                allowed=True,
                sanitized_params=sanitized,
                allowed_roots=self._allowed_roots(project_root),
            )

        if normalized_action == "open_explorer":
            raw_path = str(sanitized.get("path", "project") or "project").strip()
            lowered = " ".join(raw_path.lower().split())
            if lowered in KNOWN_FOLDERS:
                sanitized["path"] = lowered
                return SandboxDecision(allowed=True, sanitized_params=sanitized)
            try:
                resolved = resolve_safe_path(raw_path, project_root)
            except ValueError as exc:
                return SandboxDecision(allowed=False, reason=str(exc))
            sanitized["_resolved_path"] = str(resolved)
            return SandboxDecision(
                allowed=True,
                sanitized_params=sanitized,
                allowed_roots=self._allowed_roots(project_root),
            )

        if normalized_action in self._SEARCH_ACTIONS:
            query = str(sanitized.get("query", "") or "").strip()
            if not query:
                return SandboxDecision(allowed=False, reason="Search actions require a non-empty query.")
            return SandboxDecision(allowed=True, sanitized_params=sanitized)

        if normalized_action in self._URL_ACTIONS:
            raw_url = str(sanitized.get("url", "") or "").strip()
            raw_video_url = str(sanitized.get("video_url", "") or "").strip()

            if normalized_action == "play_youtube" and raw_video_url:
                result = self._validate_http_url(raw_video_url, youtube_only=True)
                if result is None:
                    return SandboxDecision(allowed=False, reason="YouTube playback only accepts youtube.com or youtu.be video URLs.")
                sanitized["video_url"] = result
                return SandboxDecision(allowed=True, sanitized_params=sanitized)

            if not raw_url and normalized_action == "open_url":
                return SandboxDecision(allowed=False, reason="URL actions require a non-empty URL.")
            if raw_url:
                result = self._validate_http_url(raw_url, youtube_only=False)
                if result is None:
                    return SandboxDecision(allowed=False, reason="Only explicit http(s) URLs are allowed.")
                sanitized["url"] = result
            return SandboxDecision(allowed=True, sanitized_params=sanitized)

        if normalized_action in {"open_app", "focus_app", "close_app", "install_app"}:
            app_name = str(sanitized.get("app_name", "") or "").strip().lower()
            sanitized["app_name"] = app_name
            return SandboxDecision(allowed=bool(app_name), reason="Sandbox policy validated." if app_name else "Application name is required.", sanitized_params=sanitized)

        if normalized_action == "system_action":
            action_type = str(sanitized.get("action_type", "") or "").strip().lower()
            if action_type not in {"shutdown", "restart", "reboot"}:
                return SandboxDecision(allowed=False, reason="Unsupported system action.")
            sanitized["action_type"] = action_type
            return SandboxDecision(allowed=True, sanitized_params=sanitized)

        return SandboxDecision(allowed=True, sanitized_params=sanitized)

    @staticmethod
    def _allowed_roots(project_root: Path) -> tuple[str, ...]:
        home = Path.home()
        candidates = (
            project_root.resolve(),
            (home / "Desktop").resolve(),
            (home / "Documents").resolve(),
            (home / "Downloads").resolve(),
        )
        return tuple(str(item) for item in candidates)

    @staticmethod
    def _validate_http_url(candidate: str, *, youtube_only: bool) -> str | None:
        normalized = normalize_url(candidate)
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        if youtube_only and parsed.netloc.lower() not in _YOUTUBE_HOSTS:
            return None
        return normalized
