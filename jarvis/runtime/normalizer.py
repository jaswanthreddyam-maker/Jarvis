from __future__ import annotations

import re
import difflib
from dataclasses import dataclass, field


@dataclass(slots=True)
class NormalizedResult:
    text: str
    metadata: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.text


class InputNormalizer:
    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._aliases = aliases or {}

        self._asr_fixes = {
            "jardubus": "jarvis",
            "woping": "open",
            "yutube": "youtube",
            "gogle": "google",
            "crome": "chrome",
        }

        # Stronger wake pattern
        self._wake_pattern = re.compile(
            r'^(?:hey\s+|hi\s+)?jarvis[\s,]*',
            re.IGNORECASE
        )

        # Multi-pass filler removal
        self._filler_pattern = re.compile(
            r'^(please|can you|could you|would you|just|kindly|go ahead and|hey|hi|hello)\b\s*',
            re.IGNORECASE
        )

        self._dedupe_tokens = {"open", "play", "go", "stop", "do", "search", "show"}

        self._known_phrases = [
            "google chrome",
            "visual studio code",
            "stack overflow"
        ]

    def update_aliases(self, aliases: dict[str, str]) -> None:
        self._aliases.update(aliases)

    def _remove_fillers(self, text: str) -> str:
        """Repeatedly remove fillers from start"""
        while True:
            new_text = self._filler_pattern.sub('', text).strip()
            if new_text == text:
                return text
            text = new_text

    def normalize(self, text: str) -> NormalizedResult:
        text = str(text).strip()

        if not text or len(text) < 2:
            return NormalizedResult(text="")

        metadata = {
            "original": text
        }

        text = text.lower()

        # Wake word extraction
        wake_match = self._wake_pattern.match(text)
        if wake_match:
            metadata["wake_word"] = "jarvis"
            text = text[wake_match.end():].strip()

        # ASR fixes
        for bad, good in self._asr_fixes.items():
            text = re.sub(rf'\b{bad}\b', good, text)

        # Protect phrases (with word boundaries)
        for phrase in self._known_phrases:
            pattern = rf'\b{re.escape(phrase)}\b'
            text = re.sub(pattern, phrase.replace(" ", "_"), text)

        # Remove punctuation
        text = re.sub(r'[^\w\s_]', '', text)

        # Remove fillers (multi-pass)
        text = self._remove_fillers(text)

        # Normalize spacing
        text = " ".join(text.split())

        words = text.split()
        expanded = []

        valid_aliases = list(self._aliases.keys())
        fuzzy_used = False

        for w in words:
            # Restore phrases
            if "_" in w:
                expanded.extend(w.split("_"))
                continue

            if w in self._aliases:
                w = self._aliases[w]

            elif self._aliases and len(w) >= 4:
                match = difflib.get_close_matches(w, valid_aliases, n=1, cutoff=0.75)
                if match:
                    w = self._aliases[match[0]]
                    fuzzy_used = True

            # Smarter dedupe
            if (
                not expanded or
                expanded[-1] != w or
                w not in self._dedupe_tokens
            ):
                expanded.append(w)

        final_text = " ".join(expanded)

        metadata["fuzzy_used"] = str(fuzzy_used)

        return NormalizedResult(text=final_text, metadata=metadata)
