"""Intent Router — classifies user input as LOCAL_TASK or ONLINE_QUERY.

Routes commands to the correct execution pipeline:
- LOCAL_TASK  → local planner / action engine (instant, offline)
- ONLINE_QUERY → remote LLM API (knowledge, reasoning, coding)

Privacy: system commands and file paths are NEVER sent to online APIs.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("Jarvis.IntentRouter")


class IntentType(str, Enum):
    LOCAL_TASK = "LOCAL_TASK"
    ONLINE_QUERY = "ONLINE_QUERY"
    CONCEPTUAL_QUERY = "CONCEPTUAL_QUERY"


@dataclass(slots=True)
class RoutingDecision:
    """Result of classifying a user utterance."""
    intent_type: IntentType
    confidence: float          # 0.0 – 1.0
    reason: str
    privacy_safe: bool = True  # False if sensitive content detected
    cached: bool = False


# ── Pattern banks ────────────────────────────────────────────────────
_LOCAL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bopen\b", re.I), "app/url launch"),
    (re.compile(r"\blaunch\b", re.I), "app launch"),
    (re.compile(r"\bsearch\s+for\b", re.I), "web search"),
    (re.compile(r"\bsearch\b", re.I), "web search"),
    (re.compile(r"\bclose\b", re.I), "system command"),
    (re.compile(r"\bshutdown\b|\brestart\b|\breboot\b", re.I), "system command"),
    (re.compile(r"\btype\b", re.I), "keyboard automation"),
    (re.compile(r"\bclick\b", re.I), "mouse automation"),
    (re.compile(r"\bscroll\b", re.I), "mouse automation"),
    (re.compile(r"\bswitch\s+to\b", re.I), "window switch"),
    (re.compile(r"\bminimize\b|\bmaximize\b", re.I), "window management"),
    (re.compile(r"\bvolume\b", re.I), "system control"),
    (re.compile(r"\bbright(ness)?\b", re.I), "system control"),
    (re.compile(r"\bremind\s+me\b", re.I), "reminder"),
    (re.compile(r"\bremember\s+that\b", re.I), "memory store"),
    (re.compile(r"\bwhat\s+do\s+you\s+remember\b", re.I), "memory recall"),
    (re.compile(r"\bwhat\s+time\b|\b\btime\b$", re.I), "time query"),
    (re.compile(r"\bhealth\s*check\b", re.I), "health check"),
    (re.compile(r"\bcapabilities\b|\bwhat\s+can\s+you\s+do\b", re.I), "capabilities"),
    (re.compile(r"\byoutube\b", re.I), "youtube"),
    (re.compile(r"\bcopy\b|\bpaste\b|\bundo\b|\bredo\b", re.I), "keyboard shortcut"),
    (re.compile(r"\bscreenshot\b", re.I), "screen capture"),
    (re.compile(r"\btask\s*manager\b", re.I), "system tool"),
    (re.compile(r"\bsettings\b|\bcontrol\s*panel\b", re.I), "system tool"),
]

_ONLINE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bwhy\b", re.I), "reasoning"),
    (re.compile(r"\bhow\s+does\b|\bhow\s+do\b", re.I), "reasoning"),
    (re.compile(r"\bwrite\b.*\b(code|script|function|program)\b", re.I), "code generation"),
    (re.compile(r"\bgenerate\b", re.I), "content generation"),
    (re.compile(r"\bsummarize\b|\bsummary\b", re.I), "summarization"),
    (re.compile(r"\btranslate\b", re.I), "translation"),
    (re.compile(r"\bcompare\b", re.I), "analysis"),
    (re.compile(r"\banalyze\b|\banalysis\b", re.I), "analysis"),
    (re.compile(r"\bstory\b|\bpoem\b|\bessay\b", re.I), "creative writing"),
    (re.compile(r"\bdefine\b|\bdefinition\b", re.I), "definition"),
    (re.compile(r"\blist\s+\d+\b|\btop\s+\d+\b", re.I), "enumeration"),
    (re.compile(r"\badvice\b|\brecommend\b|\bsuggest\b", re.I), "advisory"),
    (re.compile(r"\bcalculate\b|\bsolve\b", re.I), "math"),
    (re.compile(r"\btell\s+me\s+about\b", re.I), "knowledge request"),
    (re.compile(r"\bwhat\s+are\b.*\b(benefits|advantages|differences)\b", re.I), "analysis"),
]

_CONCEPTUAL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bexplain\b", re.I), "concept explanation"),
    (re.compile(r"\bwhat\s+is\b", re.I), "concept definition"),
    (re.compile(r"\bwho\s+is\b|\bwho\s+was\b", re.I), "entity concept"),
    (re.compile(r"\bhow\s+are\b.*\brelated\b", re.I), "concept relation"),
    (re.compile(r"\bconnection\s+between\b", re.I), "concept relation"),
]

# ── Privacy-sensitive patterns (never send online) ───────────────────
_SENSITIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"[A-Z]:\\", re.I),                    # Windows file paths
    re.compile(r"/home/|/usr/|/etc/", re.I),          # Unix file paths
    re.compile(r"\bpassword\b|\bsecret\b|\btoken\b|\bapi\s*key\b", re.I),
    re.compile(r"\bdelete\b|\bremove\b|\bformat\b", re.I),
    re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"),     # phone numbers
    re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),  # card numbers
]

_EXACT_LOCAL_SHORTCUTS: dict[str, str] = {
    "open youtube": "exact launcher command",
    "open google": "exact launcher command",
    "open chrome": "exact launcher command",
    "what time is it": "exact time command",
    "what is the time": "exact time command",
}
_OPEN_SHORTCUT_TARGETS = {
    "youtube",
    "google",
    "chrome",
    "gmail",
    "github",
    "spotify",
    "reddit",
    "firefox",
    "notepad",
    "terminal",
}
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
_QUESTION_PREFIXES = (
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "can",
    "could",
    "would",
    "should",
    "is",
    "are",
    "do",
    "does",
)


class _ResponseCache:
    """LRU cache for online query responses to avoid repeated API calls."""

    def __init__(self, max_size: int = 128, ttl_seconds: float = 3600.0):
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds

    def _key(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", text.strip().lower())
        return hashlib.md5(normalized.encode()).hexdigest()

    def get(self, text: str) -> str | None:
        key = self._key(text)
        if key in self._cache:
            value, ts = self._cache[key]
            if time.time() - ts < self._ttl:
                self._cache.move_to_end(key)
                return value
            del self._cache[key]
        return None

    def put(self, text: str, response: str) -> None:
        key = self._key(text)
        self._cache[key] = (response, time.time())
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)


class IntentRouter:
    """Classifies user input and routes to local or online pipeline."""

    CONFIDENCE_THRESHOLD = 0.55  # below this → escalate to online

    def __init__(self) -> None:
        self._cache = _ResponseCache()

    def classify(self, text: str) -> RoutingDecision:
        stripped = text.strip()
        if not stripped:
            return RoutingDecision(
                intent_type=IntentType.LOCAL_TASK,
                confidence=1.0,
                reason="empty input",
            )

        privacy_safe = self._check_privacy(stripped)
        shortcut_reason = self._shortcut_reason(stripped)
        if shortcut_reason:
            return RoutingDecision(
                intent_type=IntentType.LOCAL_TASK,
                confidence=1.0,
                reason=shortcut_reason,
                privacy_safe=privacy_safe,
            )

        local_score, local_reason = self._score_patterns(stripped, _LOCAL_PATTERNS)
        online_score, online_reason = self._score_patterns(stripped, _ONLINE_PATTERNS)
        concept_score, concept_reason = self._score_patterns(stripped, _CONCEPTUAL_PATTERNS)
        normalized = self._normalize_text(stripped)

        if self._is_conversational_online(stripped, normalized, local_score):
            cached = self._cache.get(stripped) is not None
            return RoutingDecision(
                intent_type=IntentType.ONLINE_QUERY,
                confidence=0.92,
                reason="greeting or conversational input",
                privacy_safe=privacy_safe,
                cached=cached,
            )

        if not privacy_safe:
            return RoutingDecision(
                intent_type=IntentType.LOCAL_TASK,
                confidence=1.0,
                reason="sensitive content detected - forced local",
                privacy_safe=False,
            )

        word_count = len(stripped.split())
        if word_count <= 4:
            if local_score > 0:
                local_score += 0.15
            else:
                online_score += 0.20
        elif word_count >= 10:
            online_score += 0.15

        max_score = max(local_score, online_score, concept_score)
        if max_score == 0:
            intent_type = IntentType.ONLINE_QUERY
            confidence = 0.5
            reason = "ambiguous - defaulting to online"
        elif max_score == concept_score:
            intent_type = IntentType.CONCEPTUAL_QUERY
            confidence = min(1.0, 0.5 + concept_score)
            reason = concept_reason or "pattern match"
        elif max_score == local_score:
            intent_type = IntentType.LOCAL_TASK
            confidence = min(1.0, 0.5 + local_score)
            reason = local_reason or "pattern match"
        else:
            intent_type = IntentType.ONLINE_QUERY
            confidence = min(1.0, 0.5 + online_score)
            reason = online_reason or "pattern match"

        # Check if cached
        cached = self._cache.get(stripped) is not None

        logger.debug(
            "Route: %s (%.2f) — %s%s",
            intent_type.value, confidence, reason,
            " [cached]" if cached else "",
        )
        return RoutingDecision(
            intent_type=intent_type,
            confidence=confidence,
            reason=reason,
            privacy_safe=privacy_safe,
            cached=cached,
        )

    def get_cached_response(self, text: str) -> str | None:
        return self._cache.get(text)

    def cache_response(self, text: str, response: str) -> None:
        self._cache.put(text, response)

    @staticmethod
    def _score_patterns(
        text: str, patterns: list[tuple[re.Pattern, str]]
    ) -> tuple[float, str]:
        total = 0.0
        best_reason = ""
        for pattern, reason in patterns:
            if pattern.search(text):
                total += 0.3
                if not best_reason:
                    best_reason = reason
        return total, best_reason

    @staticmethod
    def _check_privacy(text: str) -> bool:
        for pattern in _SENSITIVE_PATTERNS:
            if pattern.search(text):
                return False
        return True

    @staticmethod
    def _normalize_text(text: str) -> str:
        normalized = re.sub(r"\s+", " ", text.strip().lower())
        return normalized.rstrip(".,!?")

    @classmethod
    def _is_conversational_online(cls, raw_text: str, normalized: str, local_score: float) -> bool:
        if not normalized:
            return False
        if normalized in _GREETINGS:
            return True
        if normalized.startswith(("hey ", "hi ", "hello ")):
            return True
        if local_score > 0:
            return False

        words = normalized.split()
        if raw_text.strip().endswith("?"):
            return True
        if words and words[0] in _QUESTION_PREFIXES:
            return True
        if len(words) <= 3:
            return True
        return False

    @staticmethod
    def _shortcut_reason(text: str) -> str:
        normalized = re.sub(r"\s+", " ", text.strip().lower())
        if normalized in _EXACT_LOCAL_SHORTCUTS:
            return _EXACT_LOCAL_SHORTCUTS[normalized]
        if normalized.startswith("open "):
            target = normalized[5:].strip(" .?!")
            if target in _OPEN_SHORTCUT_TARGETS:
                return f"shortcut open command for {target}"
        return ""
