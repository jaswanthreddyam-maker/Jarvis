from __future__ import annotations

from assistant.command_router import CommandIntent
from assistant.context_manager import resolve_context


_BROWSER_APPS = frozenset({"chrome", "google chrome", "comet", "edge", "microsoft edge", "firefox"})
_YOUTUBE_URL_HINTS = ("youtube.com", "youtu.be")


class ContextContinuityEngine:
    """Applies cross-command continuity and active-app aware intent chaining."""

    def enrich(
        self,
        intents: list[CommandIntent],
        *,
        memory=None,
        last_app: str = "",
        _last_action: str = "",
        _last_target: str = "",
    ) -> tuple[list[CommandIntent], str | None]:
        active_app = last_app.strip().lower()
        snapshot = memory.snapshot() if hasattr(memory, "snapshot") else memory
        enriched: list[CommandIntent] = []

        for intent in intents:
            previous = enriched[-1] if enriched else None
            action = intent.action

            if action == "open_app":
                active_app = str(intent.params.get("app_name", intent.target)).strip().lower()
                enriched.append(intent)
                continue

            if action == "open_url":
                active_app = self._surface_for_open_url(intent) or active_app
                if active_app in _BROWSER_APPS:
                    intent.params.setdefault("browser_app", active_app)
                    intent.description = self._describe_with_browser(intent, active_app)
                enriched.append(intent)
                continue

            if action in {"search_youtube", "play_youtube"}:
                if active_app != "youtube" and not self._opens_surface(previous, "youtube"):
                    open_youtube = CommandIntent(
                        action="open_url",
                        target="youtube",
                        params={"url": "https://www.youtube.com"},
                        description="Open youtube in the browser.",
                        confidence=intent.confidence,
                    )
                    enriched.append(open_youtube)
                    previous = open_youtube
                    active_app = "youtube"
                if self._opens_surface(previous, "youtube"):
                    intent.depends_on_previous = True
                intent.description = (
                    self._describe_youtube_play(intent)
                    if action == "play_youtube"
                    else self._describe_youtube_search(intent)
                )
                active_app = "youtube"
                enriched.append(intent)
                continue

            if action == "search_web" and active_app == "youtube":
                intent.action = "search_youtube"
                intent.params.pop("browser_app", None)
                if self._opens_surface(previous, "youtube"):
                    intent.depends_on_previous = True
                intent.description = self._describe_youtube_search(intent)
                active_app = "youtube"
                enriched.append(intent)
                continue

            if action in {"search_web", "open_url"}:
                resolution = resolve_context(
                    snapshot,
                    requested_action=action,
                    require_browser=True,
                )
                if resolution.clarification_question and previous is None:
                    return intents, resolution.clarification_question

                browser_app = self._browser_app_for(previous, active_app)
                if resolution.selected is not None:
                    resolved_browser = str(resolution.selected.primary_app).strip().lower()
                    if resolved_browser:
                        browser_app = resolved_browser

                if browser_app:
                    intent.params.setdefault("browser_app", browser_app)
                    if previous is not None and previous.action == "open_app":
                        previous_app = str(previous.params.get("app_name", previous.target)).strip().lower()
                        if previous_app == browser_app:
                            intent.depends_on_previous = True
                            intent.param_bindings["browser_app"] = f"step_{len(enriched)}.data.app_name"
                    intent.description = self._describe_with_browser(intent, browser_app)
                active_app = browser_app or active_app
                enriched.append(intent)
                continue

            if action in {"focus_app", "close_app"}:
                active_app = str(intent.params.get("app_name", intent.target)).strip().lower()

            enriched.append(intent)

        return enriched, None

    @staticmethod
    def _browser_app_for(previous: CommandIntent | None, active_app: str) -> str:
        if previous is not None and previous.action == "open_app":
            candidate = str(previous.params.get("app_name", previous.target)).strip().lower()
            if candidate in _BROWSER_APPS:
                return candidate
        if active_app in _BROWSER_APPS:
            return active_app
        return ""

    @staticmethod
    def _surface_for_open_url(intent: CommandIntent) -> str:
        target = str(intent.target).strip().lower()
        url = str(intent.params.get("url", "")).strip().lower()
        if target == "youtube" or any(hint in url for hint in _YOUTUBE_URL_HINTS):
            return "youtube"
        return ""

    @staticmethod
    def _opens_surface(intent: CommandIntent | None, surface: str) -> bool:
        if intent is None:
            return False
        if surface == "youtube":
            return intent.action == "open_url" and ContextContinuityEngine._surface_for_open_url(intent) == "youtube"
        return False

    @staticmethod
    def _describe_with_browser(intent: CommandIntent, browser_app: str) -> str:
        target = intent.target or str(intent.params.get("query") or intent.params.get("url") or "").strip()
        if intent.action == "search_web":
            return f"Search the web for {target} in {browser_app}."
        if intent.action == "open_url":
            return f"Open {target} in {browser_app}."
        return intent.description

    @staticmethod
    def _describe_youtube_search(intent: CommandIntent) -> str:
        target = intent.target or str(intent.params.get("query", "")).strip()
        return f"Search YouTube for {target}."

    @staticmethod
    def _describe_youtube_play(intent: CommandIntent) -> str:
        target = intent.target or str(intent.params.get("query", "")).strip()
        return f"Play YouTube result for {target}."
