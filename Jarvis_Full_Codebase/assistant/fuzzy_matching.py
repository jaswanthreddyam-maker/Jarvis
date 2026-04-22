from __future__ import annotations

try:
    from rapidfuzz import fuzz as _rffuzz
    from rapidfuzz import process as _rfprocess
except ImportError:
    from difflib import SequenceMatcher

    _RAPIDFUZZ = False
else:
    _RAPIDFUZZ = True

from assistant.tools.app_control import APP_ALIASES


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def format_app_name(name: str) -> str:
    parts = [part for part in _normalize(name).split(" ") if part]
    return " ".join(part.capitalize() for part in parts)


class FuzzyAppMatcher:
    def __init__(self, known_apps: list[str] | None = None) -> None:
        entries = known_apps or sorted(APP_ALIASES)
        self._known_apps = [_normalize(item) for item in entries if _normalize(item)]

    def suggest(self, app_name: str, *, min_score: float = 0.72) -> str:
        candidate = _normalize(app_name)
        if not candidate or candidate in self._known_apps:
            return ""

        if _RAPIDFUZZ:
            match = _rfprocess.extractOne(
                candidate,
                self._known_apps,
                scorer=_rffuzz.WRatio,
                score_cutoff=min_score * 100,
            )
            return match[0] if match else ""

        best_match = ""
        best_score = 0.0
        for known_app in self._known_apps:
            score = SequenceMatcher(a=candidate, b=known_app).ratio()
            if candidate in known_app or known_app in candidate:
                score += 0.08
            if score > best_score:
                best_match = known_app
                best_score = score

        if best_score < min_score:
            return ""
        return best_match

    def build_prompt(self, app_name: str) -> str:
        suggestion = self.suggest(app_name)
        if not suggestion:
            return ""
        return f"Did you mean {format_app_name(suggestion)}?"
