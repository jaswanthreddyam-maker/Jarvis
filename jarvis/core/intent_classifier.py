"""
jarvis/core/intent_classifier.py
─────────────────────────────────
Tier-1 fast intent classifier.

Every command enters here first. If a pattern matches, the action is
dispatched IMMEDIATELY — no LLM call, no network round-trip.
Only unrecognised commands fall through to the LLM planner (Tier 2).

Target latency: < 10 ms for a match, < 1 ms for a miss.

Architecture note
─────────────────
This module is intentionally self-contained and has zero imports from
jarvis.core.brain / jarvis.core.planner so it can be tested in isolation
and loaded before the LLM client is even initialised.

How to add a new fast pattern
──────────────────────────────
1. Add an entry to _RULES in IntentClassifier.__init__().
2. Each rule is a FastRule(patterns=[...], handler=_your_handler).
3. A pattern is a compiled regex with named groups that become kwargs
   passed to the handler.
4. The handler receives (text, **named_groups) and returns a FastResult.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote_plus

logger = logging.getLogger("Jarvis.IntentClassifier")

# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class FastResult:
    """Returned when Tier-1 matches. Mimics the shape the orchestrator expects."""
    matched: bool
    tool: str = ""
    args: dict = field(default_factory=dict)
    description: str = ""
    elapsed_ms: float = 0.0


_NO_MATCH = FastResult(matched=False)


# ─────────────────────────────────────────────────────────────────────────────
# Known-value tables  (kept here so the classifier is self-contained)
# ─────────────────────────────────────────────────────────────────────────────

_KNOWN_URLS: dict[str, str] = {
    "youtube":       "https://www.youtube.com",
    "google":        "https://www.google.com",
    "gmail":         "https://mail.google.com",
    "github":        "https://github.com",
    "spotify":       "https://open.spotify.com",
    "reddit":        "https://www.reddit.com",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow":"https://stackoverflow.com",
    "chatgpt":       "https://chat.openai.com",
    "openai":        "https://openai.com",
    "gemini":        "https://gemini.google.com",
    "bing":          "https://www.bing.com",
    "wikipedia":     "https://www.wikipedia.org",
    "twitter":       "https://twitter.com",
    "x":             "https://x.com",
    "facebook":      "https://www.facebook.com",
    "instagram":     "https://www.instagram.com",
    "linkedin":      "https://www.linkedin.com",
    "whatsapp":      "https://web.whatsapp.com",
    "notion":        "https://www.notion.so",
    "figma":         "https://www.figma.com",
    "discord":       "https://discord.com/app",
    "twitch":        "https://www.twitch.tv",
    "netflix":       "https://www.netflix.com",
}

# Sites that have their own search URL scheme
_SEARCH_ENGINES: dict[str, str] = {
    "youtube":   "https://www.youtube.com/search?q={q}",
    "google":    "https://www.google.com/search?q={q}",
    "reddit":    "https://www.reddit.com/search?q={q}",
    "github":    "https://github.com/search?q={q}",
    "bing":      "https://www.bing.com/search?q={q}",
    "twitter":   "https://twitter.com/search?q={q}",
    "x":         "https://x.com/search?q={q}",
    "wikipedia":  "https://en.wikipedia.org/wiki/Special:Search?search={q}",
}

_KNOWN_FOLDERS: dict[str, str] = {
    "desktop":   "desktop",
    "documents": "documents",
    "downloads": "downloads",
    "music":     "music",
    "pictures":  "pictures",
    "photos":    "pictures",
    "videos":    "videos",
    "home":      "home",
}

_BROWSER_ALIASES: dict[str, str] = {
    "chrome":          "chrome",
    "google chrome":   "chrome",
    "edge":            "edge",
    "microsoft edge":  "edge",
    "firefox":         "firefox",
    "comet":           "comet",
    "comet browser":   "comet",
}

_VOLUME_WORDS: dict[str, str] = {
    "up":       "increase",
    "increase": "increase",
    "raise":    "increase",
    "louder":   "increase",
    "higher":   "increase",
    "down":     "decrease",
    "decrease": "decrease",
    "lower":    "decrease",
    "quieter":  "decrease",
    "mute":     "mute",
    "silence":  "mute",
    "unmute":   "mute",
}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """Lowercase + collapse whitespace."""
    return " ".join(text.strip().lower().split())


def _extract_browser(phrase: str) -> str | None:
    """Return canonical browser key if phrase contains a browser name."""
    p = _norm(phrase)
    for alias, key in sorted(_BROWSER_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in p:
            return key
    return None


def _url_for_site(site: str) -> str | None:
    return _KNOWN_URLS.get(_norm(site))


def _search_url(site: str, query: str) -> str | None:
    tmpl = _SEARCH_ENGINES.get(_norm(site))
    if tmpl is None:
        return None
    return tmpl.replace("{q}", quote_plus(query.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# Rule dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FastRule:
    patterns: list[re.Pattern]
    handler: Callable[..., FastResult]
    name: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Handler functions  (one per intent category)
# ─────────────────────────────────────────────────────────────────────────────

def _h_open_url(text: str, *, site: str, rest: str = "") -> FastResult:
    """open <site>  /  open <site> in <browser>"""
    browser = _extract_browser(rest) if rest else None
    url = _url_for_site(site)
    if url is None:
        # Treat as a bare domain if it looks like one
        cleaned = _norm(site)
        if "." in cleaned and " " not in cleaned:
            url = f"https://{cleaned}"
        else:
            return _NO_MATCH
    args: dict = {"url": url}
    if browser:
        args["browser_app"] = browser
    desc = f"Open {url}" + (f" in {browser}" if browser else "") + "."
    return FastResult(matched=True, tool="open_url", args=args, description=desc)


def _h_search_on_site(text: str, *, query: str, site: str) -> FastResult:
    """search for <query> on <site>  /  search <query> in youtube"""
    url = _search_url(site, query)
    if url is None:
        # Fall back to google search mentioning the site
        url = f"https://www.google.com/search?q={quote_plus(query.strip() + ' ' + site.strip())}"
    return FastResult(
        matched=True,
        tool="open_url",
        args={"url": url},
        description=f"Search for '{query}' on {site}.",
    )


def _h_web_search(text: str, *, query: str) -> FastResult:
    """search for <query>  (no site specified → google)"""
    url = f"https://www.google.com/search?q={quote_plus(query.strip())}"
    return FastResult(
        matched=True,
        tool="open_url",
        args={"url": url},
        description=f"Search Google for '{query}'.",
    )


def _h_open_app(text: str, *, app: str, rest: str = "") -> FastResult:
    """open <app>"""
    app_name = _norm(app)
    return FastResult(
        matched=True,
        tool="open_app",
        args={"app_name": app_name},
        description=f"Open the {app_name} application.",
    )


def _h_open_folder(text: str, *, folder: str) -> FastResult:
    """open downloads / open documents folder"""
    key = _norm(folder)
    canonical = _KNOWN_FOLDERS.get(key, key)
    return FastResult(
        matched=True,
        tool="open_explorer",
        args={"path": canonical},
        description=f"Open the {canonical} folder.",
    )


def _h_create_folder(text: str, *, name: str) -> FastResult:
    return FastResult(
        matched=True,
        tool="create_folder",
        args={"name": name.strip()},
        description=f"Create folder '{name.strip()}'.",
    )


def _h_delete_folder(text: str, *, name: str) -> FastResult:
    return FastResult(
        matched=True,
        tool="delete_folder",
        args={"name": name.strip()},
        description=f"Delete folder '{name.strip()}'.",
    )


def _h_volume(text: str, *, direction: str) -> FastResult:
    cmd = _VOLUME_WORDS.get(_norm(direction), "")
    if not cmd:
        return _NO_MATCH
    return FastResult(
        matched=True,
        tool="set_volume",
        args={"value": cmd},
        description=f"Volume {cmd}.",
    )


def _h_screenshot(text: str) -> FastResult:
    return FastResult(
        matched=True,
        tool="take_screenshot",
        args={},
        description="Take a screenshot.",
    )


def _h_clipboard_get(text: str) -> FastResult:
    return FastResult(
        matched=True,
        tool="get_clipboard",
        args={},
        description="Read the clipboard.",
    )


def _h_clipboard_set(text: str, *, content: str) -> FastResult:
    return FastResult(
        matched=True,
        tool="set_clipboard",
        args={"text": content.strip()},
        description=f"Copy '{content.strip()}' to clipboard.",
    )


def _h_get_time(text: str) -> FastResult:
    return FastResult(
        matched=True,
        tool="get_time",
        args={},
        description="Get the current time.",
    )


def _h_minimize(text: str) -> FastResult:
    return FastResult(
        matched=True,
        tool="minimize_window",
        args={},
        description="Minimize the current window.",
    )


def _h_close_app(text: str, *, app: str) -> FastResult:
    return FastResult(
        matched=True,
        tool="close_app",
        args={"app_name": _norm(app)},
        description=f"Close {_norm(app)}.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Classifier
# ─────────────────────────────────────────────────────────────────────────────

class IntentClassifier:
    """
    Tier-1 fast classifier.

    Usage
    ─────
        classifier = IntentClassifier()
        result = classifier.classify("open youtube")
        if result.matched:
            # dispatch result.tool with result.args directly
        else:
            # fall through to LLM planner
    """

    def __init__(self) -> None:
        # Site names joined for use in regex alternation
        _sites = "|".join(re.escape(k) for k in sorted(_KNOWN_URLS, key=len, reverse=True))
        _browsers = "|".join(re.escape(k) for k in sorted(_BROWSER_ALIASES, key=len, reverse=True))
        _folders = "|".join(re.escape(k) for k in sorted(_KNOWN_FOLDERS, key=len, reverse=True))
        _vol = "|".join(re.escape(k) for k in _VOLUME_WORDS)

        def _p(pattern: str) -> re.Pattern:
            return re.compile(pattern, re.IGNORECASE)

        self._rules: list[FastRule] = [

            # ── 1. Search on a specific site ──────────────────────────────
            # "search for lofi music on youtube"
            # "search lofi music in youtube"
            # "find dark souls on youtube"
            # "look up python tutorials on github"
            FastRule(
                name="search_on_site",
                patterns=[
                    _p(rf"(?:search(?:\s+for)?|find|look\s+up)\s+(?P<query>.+?)\s+(?:on|in)\s+(?P<site>{_sites})\s*\.?$"),
                    _p(rf"(?P<site>{_sites})\s+search\s+(?:for\s+)?(?P<query>.+?)\s*\.?$"),
                ],
                handler=_h_search_on_site,
            ),

            # ── 2. Open a known URL in an optional browser ─────────────────
            # "open youtube"
            # "open youtube in chrome"
            # "go to reddit in comet browser"
            # "launch spotify"
            FastRule(
                name="open_known_url",
                patterns=[
                    _p(rf"(?:open|go\s+to|launch|navigate\s+to|take\s+me\s+to)\s+(?P<site>{_sites})(?:\s+(?:in|using|with|on)\s+(?P<rest>.+?))?\s*\.?$"),
                ],
                handler=_h_open_url,
            ),

            # ── 3. General web search (no site) ───────────────────────────
            # "search for how to make pasta"
            # "google best python libraries"
            # "look up dark souls wiki"
            FastRule(
                name="web_search",
                patterns=[
                    _p(r"(?:search(?:\s+for)?|google|look\s+up|find\s+online)\s+(?P<query>.+?)\s*\.?$"),
                ],
                handler=_h_web_search,
            ),

            # ── 4. Open known folder ───────────────────────────────────────
            # "open downloads"
            # "open my documents folder"
            # "go to desktop"
            FastRule(
                name="open_folder",
                patterns=[
                    _p(rf"(?:open|go\s+to|show\s+me)\s+(?:my\s+)?(?P<folder>{_folders})(?:\s+folder)?\s*\.?$"),
                ],
                handler=_h_open_folder,
            ),

            # ── 5. Open an application ────────────────────────────────────
            # "open notepad"
            # "launch spotify"
            # "start terminal"
            # Must come AFTER url+folder rules so "open youtube" doesn't land here
            FastRule(
                name="open_app",
                patterns=[
                    _p(r"(?:open|launch|start|run)\s+(?P<app>[a-zA-Z0-9_\- ]+?)\s*\.?$"),
                ],
                handler=_h_open_app,
            ),

            # ── 6. Close an application ───────────────────────────────────
            # "close spotify"
            # "quit chrome"
            FastRule(
                name="close_app",
                patterns=[
                    _p(r"(?:close|quit|exit|kill)\s+(?P<app>[a-zA-Z0-9_\- ]+?)\s*\.?$"),
                ],
                handler=_h_close_app,
            ),

            # ── 7. Create folder ─────────────────────────────────────────
            # "create a folder called Projects"
            # "make a new folder named Work"
            # "new folder MyStuff"
            FastRule(
                name="create_folder",
                patterns=[
                    _p(r"(?:create|make|new)\s+(?:a\s+)?(?:new\s+)?folder\s+(?:called|named|:)?\s*(?P<name>[^\s].+?)\s*\.?$"),
                ],
                handler=_h_create_folder,
            ),

            # ── 8. Delete folder ─────────────────────────────────────────
            FastRule(
                name="delete_folder",
                patterns=[
                    _p(r"(?:delete|remove|trash)\s+(?:the\s+)?(?:folder\s+)?(?:called|named)?\s*(?P<name>[^\s].+?)\s+folder\s*\.?$"),
                    _p(r"(?:delete|remove|trash)\s+(?:folder\s+)?(?:called\s+|named\s+)?(?P<name>[^\s].+?)\s*\.?$"),
                ],
                handler=_h_delete_folder,
            ),

            # ── 9. Volume ─────────────────────────────────────────────────
            # "volume up" / "turn up the volume" / "mute" / "unmute"
            FastRule(
                name="volume",
                patterns=[
                    _p(rf"(?:volume|vol)\s+(?P<direction>{_vol})\s*\.?$"),
                    _p(rf"(?:turn|set)?\s*(?:the\s+)?(?:volume|vol)\s+(?P<direction>{_vol})\s*\.?$"),
                    _p(rf"^(?P<direction>mute|unmute|silence)\s*\.?$"),
                    _p(rf"^(?P<direction>louder|quieter)\s*\.?$"),
                ],
                handler=_h_volume,
            ),

            # ── 10. Screenshot ────────────────────────────────────────────
            FastRule(
                name="screenshot",
                patterns=[
                    _p(r"(?:take\s+a?\s*|capture\s+a?\s*|grab\s+a?\s*)?screenshot\s*\.?$"),
                    _p(r"(?:take|capture|grab)\s+(?:a\s+)?(?:screenshot|screen\s+capture|screen\s+shot)\s*\.?$"),
                ],
                handler=lambda text: _h_screenshot(text),
            ),

            # ── 11. Clipboard ─────────────────────────────────────────────
            FastRule(
                name="clipboard_get",
                patterns=[
                    _p(r"(?:show|read|get|what(?:'s|\s+is)\s+(?:in\s+)?(?:my\s+)?(?:the\s+)?)clipboard\s*\.?$"),
                    _p(r"(?:paste|show)\s+clipboard\s*\.?$"),
                ],
                handler=lambda text: _h_clipboard_get(text),
            ),
            FastRule(
                name="clipboard_set",
                patterns=[
                    _p(r"copy\s+(?P<content>.+?)\s+to\s+(?:the\s+)?clipboard\s*\.?$"),
                    _p(r"(?:put|set)\s+(?P<content>.+?)\s+(?:in(?:to)?|on)\s+(?:the\s+)?clipboard\s*\.?$"),
                ],
                handler=_h_clipboard_set,
            ),

            # ── 12. Time / date ───────────────────────────────────────────
            FastRule(
                name="get_time",
                patterns=[
                    _p(r"what(?:'s|\s+is)\s+(?:the\s+)?(?:current\s+)?(?:time|date|day)(?:\s+(?:now|today|right\s+now|is\s+it))?\s*[?\.]?\s*$"),
                    _p(r"(?:tell\s+me\s+(?:the\s+)?)?(?:current\s+)?(?:time|date)\s*[?\.]?\s*$"),
                    _p(r"what\s+(?:time|date|day)\s+is\s+it(?:\s+(?:now|today|right\s+now))?\s*[?\.]?\s*$"),
                ],
                handler=lambda text: _h_get_time(text),
            ),

            # ── 13. Minimize window ───────────────────────────────────────
            FastRule(
                name="minimize",
                patterns=[
                    _p(r"minimize(?:\s+(?:this\s+)?(?:window|app))?\s*\.?$"),
                    _p(r"(?:hide|minimise)(?:\s+(?:this\s+)?(?:window|app))?\s*\.?$"),
                ],
                handler=lambda text: _h_minimize(text),
            ),
        ]

    # ─────────────────────────────────────────────────────────────────────────

    def classify(self, text: str) -> FastResult:
        """
        Try every rule in priority order.
        Returns a FastResult with matched=True on the first hit,
        or FastResult(matched=False) if nothing matches.
        """
        t0 = time.perf_counter()
        normalised = _norm(text)

        for rule in self._rules:
            for pattern in rule.patterns:
                m = pattern.fullmatch(normalised)
                if m is None:
                    # Also try search (not fullmatch) for patterns that can
                    # appear mid-string — but only for search rules
                    m = pattern.search(normalised)
                    if m is None:
                        continue

                groups = {k: v for k, v in m.groupdict().items() if v is not None}
                try:
                    result = rule.handler(text, **groups)
                except Exception as exc:
                    logger.warning("Fast rule '%s' handler raised: %s", rule.name, exc)
                    continue

                if result.matched:
                    result.elapsed_ms = (time.perf_counter() - t0) * 1000
                    logger.info(
                        "[FastTier] %-20s → tool=%-20s args=%s  (%.2f ms)",
                        rule.name, result.tool, result.args, result.elapsed_ms,
                    )
                    return result

        elapsed = (time.perf_counter() - t0) * 1000
        logger.debug("[FastTier] No match — falling through to LLM planner (%.2f ms)", elapsed)
        return _NO_MATCH
