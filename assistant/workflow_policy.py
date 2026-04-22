from __future__ import annotations

from dataclasses import replace
import re
from typing import Any

from assistant.contracts import StepDefinition, TaskPlan


_BROWSER_KEYWORDS = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "comet": "comet",
    "edge": "edge",
    "microsoft edge": "edge",
    "firefox": "firefox",
}
_PLATFORM_KEYWORDS = {
    "youtube": "youtube",
    "youtu.be": "youtube",
    "spotify": "spotify",
    "github": "github",
    "reddit": "reddit",
    "gmail": "gmail",
    "mail.google.com": "gmail",
    "google": "google",
}
_BINDING_RE = re.compile(r"^step_(\d+)(\..+)$")


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _display_names(values: set[str]) -> str:
    return ", ".join(sorted(value.title() for value in values))


def _find_keywords(text: str, keywords: dict[str, str]) -> set[str]:
    normalized = _normalize(text)
    matches: set[str] = set()
    for raw_keyword, canonical in keywords.items():
        pattern = rf"(?<![a-z0-9]){re.escape(raw_keyword)}(?![a-z0-9])"
        if re.search(pattern, normalized):
            matches.add(canonical)
    return matches


def _strip_browser_phrases(text: str) -> str:
    cleaned = _normalize(text)
    for raw_keyword in sorted(_BROWSER_KEYWORDS, key=len, reverse=True):
        cleaned = re.sub(rf"(?<![a-z0-9]){re.escape(raw_keyword)}(?![a-z0-9])", " ", cleaned)
    return " ".join(cleaned.split())


def _step_search_text(step: dict[str, Any] | StepDefinition) -> str:
    if isinstance(step, StepDefinition):
        params = dict(step.params)
        bits = [
            step.action,
            step.target,
            str(params.get("url", "")),
            str(params.get("query", "")),
            str(params.get("browser_app", "")),
            str(params.get("app_name", "")),
        ]
    else:
        params = dict(step.get("params", {}))
        bits = [
            str(step.get("action") or step.get("intent") or ""),
            str(step.get("target", "")),
            str(params.get("url", "")),
            str(params.get("query", "")),
            str(params.get("browser_app", "")),
            str(params.get("app_name", "")),
        ]
    return " ".join(bit for bit in bits if bit)


def build_command_context(goal: str, steps: list[dict[str, Any]] | list[StepDefinition] | None = None) -> dict[str, Any]:
    normalized_goal = _normalize(goal)
    platforms = _find_keywords(_strip_browser_phrases(normalized_goal), _PLATFORM_KEYWORDS)
    browsers = _find_keywords(normalized_goal, _BROWSER_KEYWORDS)
    actions: set[str] = set()

    for step in steps or []:
        step_text = _step_search_text(step)
        platforms.update(_find_keywords(_strip_browser_phrases(step_text), _PLATFORM_KEYWORDS))
        browsers.update(_find_keywords(step_text, _BROWSER_KEYWORDS))
        if isinstance(step, StepDefinition):
            actions.add(_normalize(step.action))
        else:
            actions.add(_normalize(str(step.get("action") or step.get("intent") or "")))

    return {
        "normalized_goal": normalized_goal,
        "platforms": sorted(platforms),
        "browsers": sorted(browsers),
        "actions": sorted(action for action in actions if action),
    }


def contexts_match(stored_context: dict[str, Any] | None, request_context: dict[str, Any] | None) -> bool:
    stored = stored_context or {}
    request = request_context or {}

    for key in ("platforms", "browsers"):
        stored_values = {str(item).strip().lower() for item in stored.get(key, []) if str(item).strip()}
        request_values = {str(item).strip().lower() for item in request.get(key, []) if str(item).strip()}
        if stored_values or request_values:
            if stored_values != request_values:
                return False
    return True


class WorkflowPolicy:
    """Applies workflow reuse constraints, conflict checks, and missing prerequisites."""

    def detect_platform_conflict(self, goal: str) -> str | None:
        context = build_command_context(goal)
        platforms = set(context["platforms"])
        browsers = set(context["browsers"])

        if len(platforms) > 1:
            return f"I found multiple platforms in that request: {_display_names(platforms)}. Which one should I use?"
        if len(browsers) > 1:
            return f"I found multiple browsers in that request: {_display_names(browsers)}. Which one should I use?"
        return None

    def enrich_plan(self, goal: str, plan: TaskPlan, *, active_app: str = "") -> TaskPlan:
        del goal
        if not plan.steps:
            return plan

        predicted: list[StepDefinition] = []
        current_surface = _normalize(active_app)

        for step in plan.steps:
            current_step = replace(step)
            if self._needs_youtube_open(current_step, predicted, current_surface):
                inserted_id = -(len(predicted) + 1)
                predicted.append(
                    StepDefinition(
                        action="open_url",
                        step_id=inserted_id,
                        target="youtube",
                        params={"url": "https://www.youtube.com"},
                        description="Open youtube in the browser.",
                        verification="confirm the URL for youtube was prepared",
                    )
                )
                current_step = replace(
                    current_step,
                    depends_on=tuple(dict.fromkeys((*current_step.depends_on, inserted_id))),
                )
                current_surface = "youtube"

            predicted.append(current_step)
            current_surface = self._surface_for_step(current_step) or current_surface

        remapped_steps = self._reindex_steps(predicted)
        return replace(plan, steps=remapped_steps)

    @staticmethod
    def _needs_youtube_open(
        step: StepDefinition,
        predicted_steps: list[StepDefinition],
        current_surface: str,
    ) -> bool:
        if _normalize(step.action) not in {"search_youtube", "play_youtube"}:
            return False
        if current_surface == "youtube":
            return False
        return not any(WorkflowPolicy._surface_for_step(candidate) == "youtube" for candidate in predicted_steps)

    @staticmethod
    def _surface_for_step(step: StepDefinition) -> str:
        action = _normalize(step.action)
        target = _normalize(step.target)
        params = dict(step.params)
        url = _normalize(str(params.get("url") or params.get("search_url") or ""))
        browser_app = _normalize(str(params.get("browser_app") or ""))
        app_name = _normalize(str(params.get("app_name") or ""))

        if action in {"search_youtube", "play_youtube"}:
            return "youtube"
        if "youtube.com" in url or "youtu.be" in url or target == "youtube":
            return "youtube"
        if browser_app:
            return browser_app
        if action in {"open_app", "focus_app"} and app_name:
            return app_name
        return ""

    @staticmethod
    def _reindex_steps(steps: list[StepDefinition]) -> list[StepDefinition]:
        id_map = {step.step_id: index for index, step in enumerate(steps, start=1)}
        reindexed: list[StepDefinition] = []

        for index, step in enumerate(steps, start=1):
            depends_on = tuple(
                id_map.get(dependency, dependency)
                for dependency in step.depends_on
                if dependency in id_map
            )
            param_bindings = {
                name: WorkflowPolicy._remap_binding(binding, id_map)
                for name, binding in step.param_bindings.items()
            }
            reindexed.append(
                replace(
                    step,
                    step_id=index,
                    depends_on=depends_on,
                    param_bindings=param_bindings,
                )
            )

        return reindexed

    @staticmethod
    def _remap_binding(binding: str, id_map: dict[int, int]) -> str:
        match = _BINDING_RE.fullmatch(binding.strip())
        if match is None:
            return binding

        source_id = int(match.group(1))
        if source_id not in id_map:
            return binding
        return f"step_{id_map[source_id]}{match.group(2)}"
