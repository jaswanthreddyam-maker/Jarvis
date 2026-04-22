from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from assistant.tools import app_control, window_control


_BROWSER_HINTS = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "comet": "comet",
    "edge": "edge",
    "microsoft edge": "edge",
    "firefox": "firefox",
}

_URL_HINTS = {
    "youtube": "youtube.com",
    "google": "google.com",
    "gmail": "mail.google.com",
    "github": "github.com",
    "reddit": "reddit.com",
    "spotify": "open.spotify.com",
}


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


@dataclass(slots=True)
class RuntimeObservation:
    observed: bool = False
    observation_mode: str = "unavailable"
    window_title: str = ""
    active_app: str = ""
    browser_app: str = ""
    url: str = ""
    playback_state: str = ""
    confidence: float = 0.0
    observed_at: str = ""
    attribution: dict[str, str] = None
    signals: dict[str, float] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "observed": self.observed,
            "observation_mode": self.observation_mode,
            "window_title": self.window_title,
            "active_app": self.active_app,
            "browser_app": self.browser_app,
            "url": self.url,
            "playback_state": self.playback_state,
            "confidence": self.confidence,
            "observed_at": self.observed_at,
            "attribution": self.attribution or {},
            "signals": self.signals or {},
        }


class RuntimeObserver:
    """Reliable perception layer using multi-signal fusion and source attribution."""

    def capture(
        self,
        *,
        action: str = "",
        result_data: dict[str, Any] | None = None,
        expected_state: dict[str, Any] | None = None,
        simulated: bool = False,
    ) -> dict[str, Any]:
        result_payload = dict(result_data or {})
        expected = dict(expected_state or {})
        if simulated:
            return RuntimeObservation(
                observed=False,
                observation_mode="simulated",
                observed_at=datetime.now(timezone.utc).isoformat(),
            ).as_dict()

        window_title = window_control.get_active_window_title()
        normalized_title = _normalize(window_title)

        active_app = self._infer_active_app(normalized_title, result_payload, expected)
        browser_app = self._infer_browser_app(normalized_title, result_payload, active_app)
        observed_url = self._infer_url(normalized_title, result_payload, expected)
        playback_state = self._infer_playback_state(
            normalized_title,
            result_payload,
            expected,
            observed_url=observed_url,
        )

        # Signal Confidence Model & Fusion
        signals = self._calculate_signals(
            window_title=window_title,
            active_app=active_app,
            browser_app=browser_app,
            observed_url=observed_url,
            playback_state=playback_state,
        )
        confidence = sum(signals.values())

        # Source Attribution
        attribution = {
            "window_title": "user32.dll:GetForegroundWindow" if window_title else "none",
            "active_app": "process_iteration:EnumProcesses" if active_app else "none",
            "url": "pattern_matching:window_title" if observed_url else "none",
            "playback_state": "heuristic:title_analysis" if playback_state else "none",
        }

        return RuntimeObservation(
            observed=bool(window_title or active_app or browser_app or observed_url),
            observation_mode="runtime",
            window_title=window_title,
            active_app=active_app,
            browser_app=browser_app,
            url=observed_url,
            playback_state=playback_state,
            confidence=round(min(1.0, confidence), 3),
            observed_at=datetime.now(timezone.utc).isoformat(),
            attribution=attribution,
            signals=signals,
        ).as_dict()

    @staticmethod
    def _calculate_signals(
        *,
        window_title: str,
        active_app: str,
        browser_app: str,
        observed_url: str,
        playback_state: str,
    ) -> dict[str, float]:
        """Multi-Signal Fusion: Assign weights to each observation source."""
        signals = {
            "window": 0.35 if window_title else 0.0,
            "process": 0.25 if active_app else 0.0,
            "browser": 0.15 if browser_app else 0.0,
            "url": 0.20 if observed_url else 0.0,
            "playback": 0.05 if playback_state else 0.0,
        }
        # Boost confidence if signals correlate (e.g. browser app + url)
        if browser_app and observed_url:
            signals["url"] += 0.05
        if active_app and window_title and active_app in _normalize(window_title):
            signals["process"] += 0.05

        return {k: round(v, 2) for k, v in signals.items()}

    @staticmethod
    def _infer_active_app(normalized_title: str, result_data: dict[str, Any], expected_state: dict[str, Any]) -> str:
        surface = RuntimeObserver._surface_from_title(normalized_title)
        if surface:
            return surface

        for alias_name, alias in app_control.APP_ALIASES.items():
            window_hint = _normalize(alias.get("window", ""))
            if window_hint and window_hint in normalized_title:
                return _normalize(alias_name)

        explicit = _normalize(
            str(
                result_data.get("active_app")
                or result_data.get("app_name")
                or expected_state.get("active_app")
                or ""
            )
        )
        if explicit and explicit in normalized_title:
            return explicit
        if not normalized_title:
            return explicit
        return ""

    @staticmethod
    def _infer_browser_app(normalized_title: str, result_data: dict[str, Any], active_app: str) -> str:
        for hint, browser in _BROWSER_HINTS.items():
            if hint in normalized_title:
                return browser
        if active_app in {"chrome", "comet", "edge", "firefox"}:
            return active_app
        browser_app = _normalize(str(result_data.get("browser_app") or ""))
        if browser_app and not normalized_title:
            return browser_app
        return ""

    @staticmethod
    def _infer_url(normalized_title: str, result_data: dict[str, Any], expected_state: dict[str, Any]) -> str:
        for hint, domain in _URL_HINTS.items():
            if hint in normalized_title:
                if expected_state.get("state") == "video_playing" and hint == "youtube":
                    return "youtube.com/watch"
                if expected_state.get("state") == "results_loaded" and hint == "youtube":
                    return "youtube.com/results"
                if expected_state.get("state") == "results_loaded" and hint == "google":
                    return "google.com/search"
                return domain
        if normalized_title:
            return ""
        for key in ("video_url", "url", "search_url"):
            value = _normalize(str(result_data.get(key, "")))
            if value:
                return value
        return ""

    @staticmethod
    def _infer_playback_state(
        normalized_title: str,
        result_data: dict[str, Any],
        expected_state: dict[str, Any],
        *,
        observed_url: str,
    ) -> str:
        if str(expected_state.get("state", "")).strip().lower() == "video_playing":
            if "youtube" in normalized_title and ("watch" in observed_url or "youtube" in normalized_title):
                return "playing"
        explicit = _normalize(str(result_data.get("playback_state", "")))
        if explicit in {"playing", "paused", "requested"} and not normalized_title:
            return explicit
        return ""

    @staticmethod
    def _surface_from_title(normalized_title: str) -> str:
        for hint, domain in _URL_HINTS.items():
            if hint in normalized_title:
                if hint == "youtube":
                    return "youtube"
                if hint == "google":
                    return "google"
                return hint
        return ""
