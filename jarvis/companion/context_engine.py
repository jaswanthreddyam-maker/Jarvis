"""
Context Engine — conversation intelligence and signal interpretation.

The same sentence can mean different things depending on context. This module
interprets user input through multiple signal layers and produces a unified
ContextFrame that influences response generation, memory creation, tone
selection, and tool usage decisions.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("Companion.ContextEngine")


@dataclass(slots=True)
class ContextFrame:
    """Complete context interpretation for a single user turn."""
    # Raw input
    raw_text: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Interpreted signals
    sentiment: str = "neutral"               # happy, sad, frustrated, anxious, excited, neutral
    urgency: float = 0.0                     # 0.0 = casual, 1.0 = urgent
    user_energy: float = 0.5                 # 0.0 = low/tired, 1.0 = high
    is_emotional: bool = False
    is_question: bool = False
    is_command: bool = False
    is_continuation: bool = False            # follows up on previous topic
    is_topic_shift: bool = False
    hesitation_detected: bool = False        # "um", "like", pauses
    interruption: bool = False               # user interrupted assistant
    silence_duration_ms: int = 0             # silence before this turn

    # Contextual metadata
    time_of_day: str = "afternoon"           # morning, afternoon, evening, late_night
    session_duration_minutes: float = 0.0
    turn_number: int = 0
    unresolved_topics: list[str] = field(default_factory=list)

    # Derived decisions
    should_clarify: bool = False
    should_remember: bool = False
    memory_importance: float = 0.0
    suggested_tone: str = "neutral"
    suggested_tools: list[str] = field(default_factory=list)

    def as_signals(self) -> dict[str, Any]:
        """Flatten into dict for persona adaptation."""
        return {
            "sentiment": self.sentiment,
            "urgency": self.urgency,
            "user_energy": self.user_energy,
            "is_emotional": self.is_emotional,
            "time_of_day": self.time_of_day,
            "is_continuation": self.is_continuation,
            "hesitation_detected": self.hesitation_detected,
            "interruption": self.interruption,
        }


# ── Signal Detectors ─────────────────────────────────────────────────────────

_URGENCY_PATTERNS = [
    (re.compile(r"\b(urgent|asap|emergency|right now|immediately|hurry)\b", re.I), 0.9),
    (re.compile(r"\b(quickly|fast|soon|need to)\b", re.I), 0.6),
    (re.compile(r"!{2,}"), 0.5),
    (re.compile(r"\b(please help|help me)\b", re.I), 0.5),
]

_EMOTION_PATTERNS = {
    "happy": re.compile(r"\b(happy|glad|great|awesome|amazing|love|wonderful|fantastic|yay)\b|[😊😄🎉❤️😍🥰]+", re.I),
    "sad": re.compile(r"\b(sad|down|depressed|upset|crying|miss|lonely|heartbroken)\b|[😢😭💔😞]+", re.I),
    "frustrated": re.compile(r"\b(frustrated|annoyed|angry|mad|irritated|ugh|hate|stupid)\b|[😤😡🤬]+", re.I),
    "anxious": re.compile(r"\b(anxious|worried|nervous|scared|stress|overwhelm|panic)\b|[😰😨😟]+", re.I),
    "excited": re.compile(r"\b(excited|can't wait|pumped|stoked|thrilled|omg)\b|[🔥🎉🤩]+", re.I),
    "grateful": re.compile(r"\b(thank|thanks|grateful|appreciate|means a lot)\b|[🙏❤️]+", re.I),
    "surprised": re.compile(r"\b(wow|what|really|no way|seriously|oh my)\b|[😮😲🤯]+", re.I),
}

_HESITATION_PATTERNS = re.compile(r"\b(um+|uh+|hmm+|like,|well,|so,|i mean|i guess|kinda|sort of)\b", re.I)

_QUESTION_PATTERNS = re.compile(r"\?|^(what|how|why|when|where|who|can you|could you|do you|is it|are you|will)\b", re.I)

_COMMAND_PATTERNS = re.compile(r"^(open|close|search|play|set|create|delete|run|show|find|tell me|remind)\b", re.I)

_REMEMBER_PATTERNS = re.compile(
    r"\b(remember|don't forget|keep in mind|note that|my .+ is|i always|i never|i prefer|i like|i hate|i'm from|my name)\b", re.I
)


class ContextEngine:
    """
    Interprets each user turn through multiple signal layers and produces
    a ContextFrame that drives all downstream decisions.
    """

    def __init__(self) -> None:
        self._session_start = time.time()
        self._turn_count = 0
        self._last_user_text = ""
        self._last_topics: list[str] = []

    def interpret(
        self,
        text: str,
        *,
        interrupted: bool = False,
        silence_ms: int = 0,
        unresolved_threads: list[str] | None = None,
        voice_mode: bool = False,
    ) -> ContextFrame:
        """Analyze a user turn and produce a full ContextFrame."""
        self._turn_count += 1
        clean = text.strip()

        frame = ContextFrame(
            raw_text=clean,
            turn_number=self._turn_count,
            interruption=interrupted,
            silence_duration_ms=silence_ms,
            unresolved_topics=list(unresolved_threads or []),
        )

        # Time awareness
        frame.time_of_day = self._detect_time_of_day()
        frame.session_duration_minutes = (time.time() - self._session_start) / 60.0

        # Sentiment & emotion
        frame.sentiment = self._detect_sentiment(clean)
        frame.is_emotional = frame.sentiment not in ("neutral",)

        # Urgency
        frame.urgency = self._detect_urgency(clean)

        # Energy level (length, punctuation, caps)
        frame.user_energy = self._detect_energy(clean)

        # Hesitation
        frame.hesitation_detected = bool(_HESITATION_PATTERNS.search(clean))

        # Question vs command
        frame.is_question = bool(_QUESTION_PATTERNS.search(clean))
        frame.is_command = bool(_COMMAND_PATTERNS.match(clean))

        # Topic continuity
        frame.is_continuation = self._is_continuation(clean)
        frame.is_topic_shift = not frame.is_continuation and self._turn_count > 1

        # Memory signals
        frame.should_remember = bool(_REMEMBER_PATTERNS.search(clean))
        frame.memory_importance = self._compute_memory_importance(frame)

        # Tone suggestion
        frame.suggested_tone = self._suggest_tone(frame)

        # Store for next turn comparison
        self._last_user_text = clean

        return frame

    def reset_session(self) -> None:
        self._session_start = time.time()
        self._turn_count = 0
        self._last_user_text = ""
        self._last_topics.clear()

    def _detect_sentiment(self, text: str) -> str:
        scores: dict[str, int] = {}
        for emotion, pattern in _EMOTION_PATTERNS.items():
            matches = pattern.findall(text)
            if matches:
                scores[emotion] = len(matches)
        if not scores:
            return "neutral"
        return max(scores, key=scores.get)  # type: ignore[arg-type]

    def _detect_urgency(self, text: str) -> float:
        max_urgency = 0.0
        for pattern, score in _URGENCY_PATTERNS:
            if pattern.search(text):
                max_urgency = max(max_urgency, score)
        return max_urgency

    def _detect_energy(self, text: str) -> float:
        energy = 0.5
        # Short messages = lower energy
        if len(text) < 10:
            energy -= 0.2
        elif len(text) > 100:
            energy += 0.15
        # Caps = higher energy
        if text.isupper() and len(text) > 3:
            energy += 0.3
        # Exclamation marks
        energy += min(0.3, text.count("!") * 0.1)
        # Emoji density
        emoji_count = len(re.findall(r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF]", text))
        energy += min(0.2, emoji_count * 0.05)
        return max(0.0, min(1.0, energy))

    def _is_continuation(self, text: str) -> bool:
        if not self._last_user_text:
            return False
        # Check for continuation markers
        continuation_starts = ("also", "and", "but", "what about", "oh and", "btw", "by the way")
        lower = text.lower().strip()
        if any(lower.startswith(marker) for marker in continuation_starts):
            return True
        # Pronoun references to previous context
        if re.match(r"^(it|that|this|those|they|he|she)\b", lower):
            return True
        # Token overlap with previous turn
        prev_tokens = set(self._last_user_text.lower().split())
        curr_tokens = set(lower.split())
        if prev_tokens and curr_tokens:
            overlap = len(prev_tokens & curr_tokens) / max(1, len(curr_tokens))
            if overlap > 0.3:
                return True
        return False

    def _compute_memory_importance(self, frame: ContextFrame) -> float:
        importance = 0.1  # baseline
        if frame.should_remember:
            importance += 0.5
        if frame.is_emotional:
            importance += 0.2
        if frame.urgency > 0.5:
            importance += 0.1
        if frame.turn_number > 8:
            importance += 0.1  # longer conversations tend to be more meaningful
        return min(1.0, importance)

    def _suggest_tone(self, frame: ContextFrame) -> str:
        if frame.urgency > 0.7:
            return "serious"
        if frame.sentiment in ("sad", "anxious"):
            return "empathetic"
        if frame.sentiment in ("happy", "excited"):
            return "playful"
        if frame.sentiment == "frustrated":
            return "supportive"
        if frame.time_of_day in ("late_night", "early_morning"):
            return "calm"
        if frame.hesitation_detected:
            return "supportive"
        return "warm"

    @staticmethod
    def _detect_time_of_day() -> str:
        hour = datetime.now().hour
        if 5 <= hour < 9:
            return "early_morning"
        if 9 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 21:
            return "evening"
        return "late_night"
