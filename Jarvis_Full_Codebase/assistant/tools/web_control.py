from __future__ import annotations

import webbrowser
from urllib.parse import quote_plus

from assistant.tools.app_control import APP_ALIASES
from assistant.tools import ToolResult
import subprocess


_KNOWN_URLS = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "spotify": "https://open.spotify.com",
    "reddit": "https://www.reddit.com",
    "chatgpt": "https://chat.openai.com",
    "openai": "https://openai.com",
    "gemini": "https://gemini.google.com",
    "bing": "https://www.bing.com",
    "wikipedia": "https://www.wikipedia.org",
    "twitter": "https://twitter.com",
    "facebook": "https://www.facebook.com",
}


def normalize_url(target: str) -> str:
    cleaned = target.strip()
    lowered = cleaned.lower()
    if lowered in _KNOWN_URLS:
        return _KNOWN_URLS[lowered]
    if lowered.startswith(("http://", "https://")):
        return cleaned
    if "." in cleaned and " " not in cleaned:
        return f"https://{cleaned}"
    return cleaned


def open_url(url: str, browser_app: str | None = None) -> ToolResult:
    normalized_url = normalize_url(url)
    try:
        if browser_app:
            _open_in_browser_app(normalized_url, browser_app)
        else:
            webbrowser.open(normalized_url, new=2)
    except OSError as exc:
        return ToolResult(success=False, message=str(exc), error="browser_launch_failed")
    return ToolResult(
        success=True,
        message=f"Opened {normalized_url}.",
        data={"url": normalized_url, "browser_app": (browser_app or "").strip()},
    )


def search_google(query: str, browser_app: str | None = None) -> ToolResult:
    search_url = f"https://www.google.com/search?q={quote_plus(query.strip())}"
    try:
        if browser_app:
            _open_in_browser_app(search_url, browser_app)
        else:
            webbrowser.open(search_url, new=2)
    except OSError as exc:
        return ToolResult(success=False, message=str(exc), error="browser_launch_failed")
    return ToolResult(
        success=True,
        message=f"Searching for '{query.strip()}'.",
        data={"query": query.strip(), "search_url": search_url, "browser_app": (browser_app or "").strip()},
    )


def search_youtube(query: str, browser_app: str | None = None) -> ToolResult:
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query.strip())}"
    try:
        if browser_app:
            _open_in_browser_app(search_url, browser_app)
        else:
            webbrowser.open(search_url, new=2)
    except OSError as exc:
        return ToolResult(success=False, message=str(exc), error="browser_launch_failed")
    return ToolResult(
        success=True,
        message=f"Searching YouTube for '{query.strip()}'.",
        data={"query": query.strip(), "search_url": search_url, "browser_app": (browser_app or "").strip()},
    )


def play_youtube(query: str, browser_app: str | None = None, video_url: str | None = None) -> ToolResult:
    resolved_video_url = (video_url or "").strip()
    if resolved_video_url:
        target_url = normalize_url(resolved_video_url)
    else:
        target_url = f"https://www.youtube.com/results?search_query={quote_plus(query.strip())}"

    try:
        if browser_app:
            _open_in_browser_app(target_url, browser_app)
        else:
            webbrowser.open(target_url, new=2)
    except OSError as exc:
        return ToolResult(success=False, message=str(exc), error="browser_launch_failed")

    data = {
        "query": query.strip(),
        "url": target_url,
        "browser_app": (browser_app or "").strip(),
        "playback_state": "requested",
    }
    if resolved_video_url:
        data["video_url"] = target_url
    else:
        data["search_url"] = target_url
    return ToolResult(
        success=True,
        message=f"Requested YouTube playback for '{query.strip()}'.",
        data=data,
    )



def _open_in_browser_app(url: str, browser_app: str) -> None:
    alias = APP_ALIASES.get(browser_app.strip().lower())
    command = alias["command"] if alias is not None else browser_app.strip()
    subprocess.Popen(["cmd", "/c", "start", "", command, url], shell=False)
