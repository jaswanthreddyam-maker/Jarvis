from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assistant.context_manager import resolve_context
from assistant.fuzzy_matching import FuzzyAppMatcher
from assistant.tools.app_control import APP_ALIASES
from assistant.tools.web_control import normalize_url


_KNOWN_URLS = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "spotify": "https://open.spotify.com",
    "reddit": "https://www.reddit.com",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "chatgpt": "https://chat.openai.com",
    "openai": "https://openai.com",
    "gemini": "https://gemini.google.com",
    "bing": "https://www.bing.com",
    "wikipedia": "https://www.wikipedia.org",
    "twitter": "https://twitter.com",
    "facebook": "https://www.facebook.com",
}
_KNOWN_FOLDERS = {"desktop", "documents", "downloads", "music", "pictures", "videos", "home", "project"}
_SYSTEM_ACTIONS = {
    "shutdown": "shutdown",
    "shut down": "shutdown",
    "restart": "restart",
    "reboot": "reboot",
}
_SEGMENT_OPENERS = (
    "open ",
    "launch ",
    "start ",
    "show ",
    "search ",
    "look up ",
    "create ",
    "make ",
    "read ",
    "overwrite ",
    "replace ",
    "delete ",
    "remove ",
    "focus ",
    "switch to ",
    "bring up ",
    "close ",
    "quit ",
    "exit ",
    "increase ",
    "raise ",
    "turn up ",
    "decrease ",
    "lower ",
    "turn down ",
    "mute ",
    "silence ",
    "copy ",
    "set ",
    "remember that ",
    "what do you remember about ",
    "remind me to ",
    "what time is it",
    "what is the time",
    "health check",
    "what can you do",
    "sort ",
    "play ",
    "do it again",
    "open it",
    "open that",
    "ask ",
    "tell ",
    "type ",
    "write ",
    "navigate ",
    "go to ",
    "find ",
)
_GREETINGS = {
    "hey",
    "hi",
    "hello",
    "hey jarvis",
    "hi jarvis",
    "hello jarvis",
    "good morning",
    "good evening",
    "good afternoon",
    "what's up",
    "sup",
    "yo",
}


def _normalize_basic(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _normalize_greeting(text: str) -> str:
    return _normalize_basic(text).rstrip(".,!")


def _is_greeting(text: str) -> bool:
    normalized = _normalize_greeting(text)
    if not normalized:
        return False
    return normalized in _GREETINGS or normalized.startswith(("hey ", "hi ", "hello "))


def _load_aliases() -> dict[str, str]:
    alias_path = Path(__file__).with_name("aliases.json")
    try:
        payload = json.loads(alias_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        _normalize_basic(str(alias)): _normalize_basic(str(target))
        for alias, target in payload.items()
        if str(alias).strip() and str(target).strip()
    }


_ALIASES = _load_aliases()


def _apply_aliases(text: str) -> str:
    if not _ALIASES:
        return text
    return " ".join(_ALIASES.get(word, word) for word in text.split())


def _normalize_text(text: str) -> str:
    cleaned = text.strip().lower()
    
    # --- Language Normalization (Telugu-English support) ---
    cleaned = re.sub(r"^youtube\s+(?:lo|in)\s+(.+?)\s+search(?:\s+chey)?$", r"search youtube for \1", cleaned)
    cleaned = re.sub(r"^(.+?)\s+youtube\s+(?:lo|in)\s+search(?:\s+chey)?$", r"search youtube for \1", cleaned)
    cleaned = re.sub(r"^(.+?)\s+search\s+chey\s+youtube\s+(?:lo|in)$", r"search youtube for \1", cleaned)
    cleaned = re.sub(r"^search\s+chey\s+(.+?)\s+youtube\s+(?:lo|in)$", r"search youtube for \1", cleaned)
    cleaned = cleaned.replace("open chey", "open")
    cleaned = cleaned.replace("search chey", "search")
    cleaned = cleaned.replace(" lo ", " in ")
    cleaned = re.sub(r"\bchey\b", "", cleaned)
    
    # --- Prefix removal ---
    cleaned = re.sub(r"^(?:hey\s+jarvis|hi\s+jarvis|jarvis)\s*,?\s*", "", cleaned)
    cleaned = re.sub(r"^(?:please|kindly|just)\s+", "", cleaned)
    cleaned = re.sub(r"^(?:can you|could you|would you|will you)\s+", "", cleaned)
    cleaned = re.sub(r"^(?:i need you to|i want you to)\s+", "", cleaned)
    # --- Suffix removal ---
    cleaned = re.sub(r"\s+in\s+the\s+browser\b", "", cleaned)
    cleaned = re.sub(r"\s+right\s+now\b", "", cleaned)

    return _apply_aliases(" ".join(cleaned.split()))



@dataclass(slots=True)
class CommandIntent:
    action: str
    target: str
    params: dict[str, object]
    description: str
    route: str = "command"
    confidence: float = 1.0
    depends_on_previous: bool = False
    param_bindings: dict[str, str] = field(default_factory=dict)

    @property
    def intent(self) -> str:
        return self.action

    def as_dict(self) -> dict[str, object]:
        return {"intent": self.action, "target": self.target, **self.params}


@dataclass(slots=True)
class CommandParse:
    intents: list[CommandIntent] = field(default_factory=list)
    confidence: float = 0.0
    clarification_question: str | None = None
    normalized_text: str = ""
    unresolved_segments: list[str] = field(default_factory=list)


class CommandRouter:
    """Routes short natural-language requests to local structured actions."""

    def __init__(self, app_matcher: FuzzyAppMatcher | None = None) -> None:
        self._app_matcher = app_matcher or FuzzyAppMatcher()

    def route(self, text: str, memory: Any | None = None) -> CommandIntent | None:
        if _is_greeting(text):
            return None
        parsed = self.route_many(text, memory=memory)
        if parsed.clarification_question or len(parsed.intents) != 1:
            return None
        return parsed.intents[0]

    def route_many(self, text: str, memory: Any | None = None) -> CommandParse:
        if _is_greeting(text):
            return CommandParse(normalized_text=_normalize_greeting(text))

        normalized = _normalize_text(text)
        if not normalized:
            return CommandParse(normalized_text=normalized)

        segments = self._split_segments(normalized)
        intents: list[CommandIntent] = []
        unresolved: list[str] = []


        for index, segment in enumerate(segments, start=1):
            parsed = self._route_segment(segment, memory=memory, depends_on_previous=index > 1)
            if isinstance(parsed, str):
                return CommandParse(
                    confidence=0.0,
                    clarification_question=parsed,
                    normalized_text=normalized,
                    unresolved_segments=[segment],
                )
            if parsed is None:
                unresolved.append(segment)
                continue
            intents.append(parsed)

        if unresolved:
            understood = ", ".join(intent.description.rstrip(".") for intent in intents)
            unknown = unresolved[0]
            if understood:
                question = f"I understood {understood}, but what should I do for '{unknown}'?"
            else:
                question = f"I’m not sure how to handle '{unknown}' yet. Could you rephrase it?"
            return CommandParse(
                intents=intents,
                confidence=self._average_confidence(intents),
                clarification_question=question,
                normalized_text=normalized,
                unresolved_segments=unresolved,
            )

        return CommandParse(
            intents=intents,
            confidence=self._average_confidence(intents),
            normalized_text=normalized,
        )

    def _route_segment(
        self,
        normalized: str,
        *,
        memory: Any | None,
        depends_on_previous: bool,
    ) -> CommandIntent | str | None:
        if normalized in {"open it again", "open that again", "do it again", "again"}:
            preferred_action = "open_app" if normalized in {"open it again", "open that again"} else ""
            repeated = self._repeat_last_action(memory, preferred_action=preferred_action)
            if isinstance(repeated, str):
                return repeated
            if repeated is None:
                return "What should I repeat?"
            repeated.depends_on_previous = depends_on_previous
            return repeated

        if normalized in {"play it", "play that", "play this"}:
            playback = self._play_from_context(memory)
            if playback is None:
                return "What should I play?"
            playback.depends_on_previous = depends_on_previous
            return playback

        direct = self._route_memory_and_reminders(normalized, depends_on_previous)
        if direct is not None:
            return direct

        direct = self._route_file_operations(normalized, depends_on_previous)
        if direct is not None:
            return direct

        direct = self._route_clipboard(normalized, depends_on_previous)
        if direct is not None:
            return direct

        direct = self._route_volume(normalized, depends_on_previous)
        if direct is not None:
            return direct

        direct = self._route_windows(normalized, depends_on_previous)
        if direct is not None:
            return direct

        direct = self._route_system_actions(normalized, depends_on_previous)
        if direct is not None:
            return direct

        direct = self._route_openers(normalized, memory, depends_on_previous)
        if direct is not None:
            return direct

        if normalized in {"what time is it", "what is the time"}:
            return CommandIntent(
                action="get_time",
                target="local_time",
                params={},
                description="Get the local system time.",
                confidence=1.0,
                depends_on_previous=depends_on_previous,
            )

        if normalized == "health check":
            return CommandIntent(
                action="health_check",
                target="jarvis_runtime",
                params={},
                description="Check runtime health.",
                confidence=1.0,
                depends_on_previous=depends_on_previous,
            )

        if normalized == "what can you do":
            return CommandIntent(
                action="report_capabilities",
                target="capabilities",
                params={},
                description="List current capabilities.",
                confidence=1.0,
                depends_on_previous=depends_on_previous,
            )

        youtube_play_match = re.fullmatch(
            r"(?:play\s+youtube(?:\s+for)?\s+(.+)|play\s+(.+?)\s+on\s+youtube|play\s+(.+))",
            normalized,
        )
        if youtube_play_match:
            query = next(group for group in youtube_play_match.groups() if group).strip(" .?!")
            return CommandIntent(
                action="play_youtube",
                target=query,
                params={"query": query},
                description=f"Play YouTube result for {query}.",
                confidence=0.9,
                depends_on_previous=depends_on_previous,
            )

        youtube_search_match = re.fullmatch(
            r"(?:search youtube(?: for)?|search on youtube(?: for)?|look up on youtube)\s+(.+)",
            normalized,
        )
        if youtube_search_match:
            query = youtube_search_match.group(1).strip(" .?!")
            return CommandIntent(
                action="search_youtube",
                target=query,
                params={"query": query},
                description=f"Search YouTube for {query}.",
                confidence=0.95,
                depends_on_previous=depends_on_previous,
            )

        search_match = re.fullmatch(r"(?:search(?: the web)?(?: for)?|look up)\s+(.+)", normalized)
        if search_match:
            query = search_match.group(1).strip(" .?!")
            return CommandIntent(
                action="search_web",
                target=query,
                params={"query": query},
                description=f"Search the web for {query}.",
                confidence=0.9,
                depends_on_previous=depends_on_previous,
            )

        if normalized.startswith("sort "):
            return "How do you want me to sort that? I don't have a dedicated folder-sorting action yet."

        return None

    def _route_system_actions(
        self,
        normalized: str,
        depends_on_previous: bool,
    ) -> CommandIntent | None:
        system_match = re.fullmatch(
            r"(shutdown|shut down|restart|reboot)(?:\s+(?:the\s+)?(?:computer|pc|system|machine))?",
            normalized,
        )
        if system_match is None:
            return None

        action_type = _SYSTEM_ACTIONS[system_match.group(1)]
        return CommandIntent(
            action="system_action",
            target=action_type,
            params={"action_type": action_type},
            description=f"Run system action {action_type}.",
            confidence=0.99,
            depends_on_previous=depends_on_previous,
        )

    def _route_memory_and_reminders(
        self,
        normalized: str,
        depends_on_previous: bool,
    ) -> CommandIntent | None:
        reminder_match = re.fullmatch(
            r"remind me to\s+(.+?)\s+in\s+(\d+)\s*(second|seconds|minute|minutes|hour|hours)",
            normalized,
        )
        if reminder_match:
            message = reminder_match.group(1).strip()
            amount = int(reminder_match.group(2))
            unit = reminder_match.group(3)
            delay_seconds = self._to_seconds(amount, unit)
            return CommandIntent(
                action="set_reminder",
                target=message,
                params={"message": message, "delay_seconds": delay_seconds},
                description=f"Schedule a reminder for {message}.",
                confidence=0.95,
                depends_on_previous=depends_on_previous,
            )

        remember_match = re.fullmatch(r"remember that\s+(.+)", normalized)
        if remember_match:
            content = remember_match.group(1).strip(" .?!")
            return CommandIntent(
                action="remember_fact",
                target=content,
                params={"content": content, "namespace": "system", "category": "fact"},
                description=f"Store the memory '{content}'.",
                confidence=0.95,
                depends_on_previous=depends_on_previous,
            )

        recall_match = re.fullmatch(r"what do you remember about\s+(.+)", normalized)
        if recall_match:
            query = recall_match.group(1).strip(" .?!")
            return CommandIntent(
                action="recall_memory",
                target=query,
                params={"query": query, "namespace": "system"},
                description=f"Recall memories about {query}.",
                confidence=0.95,
                depends_on_previous=depends_on_previous,
            )
        return None

    def _route_openers(
        self,
        normalized: str,
        memory: Any | None,
        depends_on_previous: bool,
    ) -> CommandIntent | str | None:
        folder_match = re.fullmatch(r"(?:open|show)\s+(.+?)\s+folder", normalized)
        if folder_match:
            folder = folder_match.group(1).strip()
            if folder in _KNOWN_FOLDERS:
                return CommandIntent(
                    action="open_explorer",
                    target=folder,
                    params={"path": folder},
                    description=f"Open the {folder} folder.",
                    confidence=0.96,
                    depends_on_previous=depends_on_previous,
                )

        open_match = re.fullmatch(r"(?:open|launch|start|show)\s+(.+)", normalized)
        if not open_match:
            return None

        target = open_match.group(1).strip(" .?!")
        # Strip browser-context suffixes
        target = re.sub(r"\s+(?:in(?:\s+the)?\s+(?:browser|chrome|edge|firefox)|using(?:\s+the)?\s+(?:browser|chrome|edge|firefox))$", "", target).strip()
        # Strip trailing action clauses like "and ask it ...", "and search for ..."
        target = re.sub(r"\s+and\s+\S.*$", "", target).strip()

        if target in {"it", "that", "it again", "that again"}:
            repeated = self._repeat_last_action(memory, preferred_action="open_app")
            if isinstance(repeated, str):
                return repeated
            if repeated is None:
                return "What do you want me to open?"
            repeated.depends_on_previous = depends_on_previous
            return repeated

        if target in {"something", "anything"}:
            return "What do you want me to open?"

        if target in _KNOWN_FOLDERS:
            return CommandIntent(
                action="open_explorer",
                target=target,
                params={"path": target},
                description=f"Open the {target} folder.",
                confidence=0.96,
                depends_on_previous=depends_on_previous,
            )

        if target in _KNOWN_URLS:
            return CommandIntent(
                action="open_url",
                target=target,
                params={"url": _KNOWN_URLS[target]},
                description=f"Open {target} in the browser.",
                confidence=0.98,
                depends_on_previous=depends_on_previous,
            )

        if target.startswith(("http://", "https://")) or ("." in target and " " not in target):
            url = normalize_url(target)
            return CommandIntent(
                action="open_url",
                target=target,
                params={"url": url},
                description=f"Open {target} in the browser.",
                confidence=0.93,
                depends_on_previous=depends_on_previous,
            )

        if target in APP_ALIASES:
            return CommandIntent(
                action="open_app",
                target=target,
                params={"app_name": target},
                description=f"Open the {target} application.",
                confidence=0.94,
                depends_on_previous=depends_on_previous,
            )

        if " " not in target:
            suggestion = self._app_matcher.build_prompt(target)
            if suggestion:
                return suggestion
            return CommandIntent(
                action="open_app",
                target=target,
                params={"app_name": target},
                description=f"Open the {target} application.",
                confidence=0.62,
                depends_on_previous=depends_on_previous,
            )
        suggestion = self._app_matcher.build_prompt(target)
        if suggestion:
            return suggestion
        return "What do you want me to open?"

    def _route_file_operations(
        self,
        normalized: str,
        depends_on_previous: bool,
    ) -> CommandIntent | str | None:
        if re.fullmatch(r"(?:create|make)(?: a)?(?: new)? file", normalized):
            return "What should I name the file?"

        create_match = re.fullmatch(
            r"(?:create|make)(?: a)?(?: new)? file(?: named)?\s+(.+?)(?:\s+with\s+content\s+(.+))?",
            normalized,
        )
        if create_match:
            name = create_match.group(1).strip(" .?!")
            content = (create_match.group(2) or "").strip()
            params: dict[str, object] = {"name": name}
            if content:
                params["content"] = content
            return CommandIntent(
                action="create_file",
                target=name,
                params=params,
                description=f"Create the file {name}.",
                confidence=0.95,
                depends_on_previous=depends_on_previous,
            )

        read_match = re.fullmatch(r"(?:read|show)\s+(?:the\s+)?file(?: named)?\s+(.+)", normalized)
        if read_match:
            name = read_match.group(1).strip(" .?!")
            return CommandIntent(
                action="read_file",
                target=name,
                params={"name": name},
                description=f"Read the file {name}.",
                confidence=0.95,
                depends_on_previous=depends_on_previous,
            )

        overwrite_match = re.fullmatch(
            r"(?:overwrite|replace)\s+(?:the\s+)?file(?: named)?\s+(.+?)\s+(?:with|to contain)\s+(.+)",
            normalized,
        )
        if overwrite_match:
            name = overwrite_match.group(1).strip(" .?!")
            content = overwrite_match.group(2).strip()
            return CommandIntent(
                action="overwrite_file",
                target=name,
                params={"name": name, "content": content},
                description=f"Overwrite the file {name}.",
                confidence=0.95,
                depends_on_previous=depends_on_previous,
            )

        delete_match = re.fullmatch(r"(?:delete|remove)\s+(?:the\s+)?file(?: named)?\s+(.+)", normalized)
        if delete_match:
            name = delete_match.group(1).strip(" .?!")
            return CommandIntent(
                action="delete_file",
                target=name,
                params={"name": name},
                description=f"Delete the file {name}.",
                confidence=0.95,
                depends_on_previous=depends_on_previous,
            )

        return None

    def _route_volume(
        self,
        normalized: str,
        depends_on_previous: bool,
    ) -> CommandIntent | None:
        if re.fullmatch(r"(?:increase|raise|turn up)(?: the)? volume", normalized):
            return CommandIntent(
                action="set_volume",
                target="increase",
                params={"value": "increase"},
                description="Increase the system volume.",
                confidence=1.0,
                depends_on_previous=depends_on_previous,
            )
        if re.fullmatch(r"(?:decrease|lower|turn down)(?: the)? volume", normalized):
            return CommandIntent(
                action="set_volume",
                target="decrease",
                params={"value": "decrease"},
                description="Decrease the system volume.",
                confidence=1.0,
                depends_on_previous=depends_on_previous,
            )
        if re.fullmatch(r"(?:mute|silence)(?: the)? volume", normalized):
            return CommandIntent(
                action="set_volume",
                target="mute",
                params={"value": "mute"},
                description="Mute the system volume.",
                confidence=1.0,
                depends_on_previous=depends_on_previous,
            )
        return None

    def _route_clipboard(
        self,
        normalized: str,
        depends_on_previous: bool,
    ) -> CommandIntent | None:
        if normalized in {"show clipboard", "what is on the clipboard", "what's on the clipboard"}:
            return CommandIntent(
                action="get_clipboard",
                target="clipboard",
                params={},
                description="Read the clipboard.",
                confidence=1.0,
                depends_on_previous=depends_on_previous,
            )

        set_match = re.fullmatch(r"(?:copy|set)\s+(.+?)\s+(?:to|into|on)\s+(?:the\s+)?clipboard", normalized)
        if set_match:
            text = set_match.group(1).strip()
            return CommandIntent(
                action="set_clipboard",
                target=text,
                params={"text": text},
                description="Update the clipboard.",
                confidence=0.95,
                depends_on_previous=depends_on_previous,
            )
        return None

    def _route_windows(
        self,
        normalized: str,
        depends_on_previous: bool,
    ) -> CommandIntent | None:
        focus_match = re.fullmatch(r"(?:focus|switch to|bring up)\s+(.+)", normalized)
        if focus_match:
            target = focus_match.group(1).strip(" .?!")
            if target in APP_ALIASES:
                return CommandIntent(
                    action="focus_app",
                    target=target,
                    params={"app_name": target},
                    description=f"Focus the {target} application.",
                    confidence=0.92,
                    depends_on_previous=depends_on_previous,
                )
            suggestion = self._app_matcher.build_prompt(target)
            if suggestion:
                return suggestion
            return CommandIntent(
                action="switch_window",
                target=target,
                params={"title": target},
                description=f"Switch to the {target} window.",
                confidence=0.78,
                depends_on_previous=depends_on_previous,
            )

        close_match = re.fullmatch(r"(?:close|quit|exit)\s+(.+)", normalized)
        if close_match:
            target = close_match.group(1).strip(" .?!")
            if target in {"active window", "current window"}:
                return CommandIntent(
                    action="close_active_window",
                    target="active_window",
                    params={},
                    description="Close the active window.",
                    confidence=1.0,
                    depends_on_previous=depends_on_previous,
                )
            suggestion = self._app_matcher.build_prompt(target)
            if suggestion:
                return suggestion
            return CommandIntent(
                action="close_app",
                target=target,
                params={"app_name": target},
                description=f"Close {target}.",
                confidence=0.9,
                depends_on_previous=depends_on_previous,
            )

        if normalized in {"minimize window", "minimize the window", "minimize active window"}:
            return CommandIntent(
                action="minimize_window",
                target="active_window",
                params={},
                description="Minimize the active window.",
                confidence=1.0,
                depends_on_previous=depends_on_previous,
            )

        return None

    def _repeat_last_action(
        self,
        memory: Any | None,
        *,
        preferred_action: str = "",
    ) -> CommandIntent | str | None:
        snapshot = memory.snapshot() if hasattr(memory, "snapshot") else memory
        if snapshot is None:
            return None

        if preferred_action and hasattr(snapshot, "recent_contexts"):
            resolution = resolve_context(
                snapshot,
                requested_action=preferred_action,
                require_browser=preferred_action in {"search_web", "open_url"},
            )
            if resolution.clarification_question and len(resolution.alternatives) > 1 and any(
                str(getattr(record, "primary_app", "")).strip()
                for record in resolution.alternatives
            ):
                return resolution.clarification_question
            preferred_intent = self._intent_from_context(resolution.selected, preferred_action)
            if preferred_intent is not None:
                return preferred_intent

        action = str(getattr(snapshot, "last_command", "")).strip()
        target = str(getattr(snapshot, "last_target", "")).strip()
        params = dict(getattr(snapshot, "last_params", {}) or {})
        if not action:
            return None

        if not target:
            target = str(params.get("app_name") or params.get("url") or params.get("query") or "").strip()
        if not target and not params:
            return None

        description = self._description_for(action, target)
        return CommandIntent(
            action=action,
            target=target,
            params=params,
            description=description,
            route="command_memory",
            confidence=0.88,
        )

    @staticmethod
    def _play_from_context(memory: Any | None) -> CommandIntent | None:
        snapshot = memory.snapshot() if hasattr(memory, "snapshot") else memory
        if snapshot is None or not hasattr(snapshot, "recent_contexts"):
            return None

        for record in snapshot.recent_contexts:
            action = str(getattr(record, "action", "")).strip().lower()
            params = dict(getattr(record, "params", {}) or {})
            target = str(getattr(record, "target", "")).strip()
            primary_app = str(getattr(record, "primary_app", "")).strip().lower()
            if action != "search_youtube" and primary_app != "youtube":
                continue
            query = str(params.get("query") or target).strip()
            if not query:
                continue
            return CommandIntent(
                action="play_youtube",
                target=query,
                params={"query": query},
                description=f"Play YouTube result for {query}.",
                route="command_memory",
                confidence=0.84,
            )
        return None

    @staticmethod
    def _intent_from_context(record: Any, requested_action: str) -> CommandIntent | None:
        if record is None:
            return None

        app_name = str(getattr(record, "primary_app", "")).strip()
        params = dict(getattr(record, "params", {}) or {})
        target = str(getattr(record, "target", "")).strip()

        if requested_action in {"open_app", "focus_app", "close_app"} and app_name:
            return CommandIntent(
                action=requested_action,
                target=app_name,
                params={"app_name": app_name},
                description=CommandRouter._description_for(requested_action, app_name),
                route="command_memory",
                confidence=0.9,
            )

        if requested_action == "search_web":
            query = str(params.get("query") or target).strip()
            if not query:
                return None
            intent_params = {"query": query}
            if app_name:
                intent_params["browser_app"] = app_name
            return CommandIntent(
                action="search_web",
                target=query,
                params=intent_params,
                description=CommandRouter._description_for("search_web", query),
                route="command_memory",
                confidence=0.9,
            )

        if requested_action == "open_url":
            url = str(params.get("url") or target).strip()
            if not url:
                return None
            intent_params = {"url": url}
            if app_name:
                intent_params["browser_app"] = app_name
            return CommandIntent(
                action="open_url",
                target=target or url,
                params=intent_params,
                description=CommandRouter._description_for("open_url", target or url),
                route="command_memory",
                confidence=0.9,
            )
        return None

    @staticmethod
    def _description_for(action: str, target: str) -> str:
        return {
            "open_app": f"Open the {target} application.",
            "open_url": f"Open {target} in the browser.",
            "open_explorer": f"Open the {target} folder.",
            "search_web": f"Search the web for {target}.",
            "search_youtube": f"Search YouTube for {target}.",
            "focus_app": f"Focus the {target} application.",
            "close_app": f"Close {target}.",
            "create_file": f"Create the file {target}.",
        }.get(action, f"Repeat {action.replace('_', ' ')} for {target}.")

    @staticmethod
    def _split_segments(normalized: str) -> list[str]:
        if not normalized:
            return []

        segments: list[str] = []
        remainder = normalized
        while remainder:
            match = re.search(r"\s+(?:and then|then|after that|afterwards|and)\s+", remainder)
            if match is None:
                segments.append(remainder.strip(" ,"))
                break

            current = remainder[: match.start()].strip(" ,")
            tail = remainder[match.end() :].strip()
            if current and tail and CommandRouter._looks_like_segment(tail):
                segments.append(current)
                remainder = tail
                continue

            segments.append(remainder.strip(" ,"))
            break
        return [segment for segment in segments if segment]

    @staticmethod
    def _looks_like_segment(text: str) -> bool:
        lowered = text.strip().lower()
        return any(lowered.startswith(prefix) for prefix in _SEGMENT_OPENERS)

    @staticmethod
    def _average_confidence(intents: list[CommandIntent]) -> float:
        if not intents:
            return 0.0
        return sum(intent.confidence for intent in intents) / len(intents)

    @staticmethod
    def _to_seconds(amount: int, unit: str) -> int:
        lowered = unit.lower()
        if lowered.startswith("hour"):
            return amount * 3600
        if lowered.startswith("minute"):
            return amount * 60
        return amount
