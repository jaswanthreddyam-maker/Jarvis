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

        # 1. Wake word pattern
        self._wake_pattern = re.compile(r'^(jarvis|hey jarvis|hi jarvis)\b', re.IGNORECASE)

        # 2. Only remove fillers at START of sentence
        self._filler_pattern = re.compile(
            r'^(?:please|can you|could you|would you|just|kindly|go ahead and|hey|hi|hello)\b',
            re.IGNORECASE
        )

        # 3. Safe dedupe verbs
        self._dedupe_tokens = {"open", "play", "go", "stop", "do", "search", "show"}

        # 4. Known multi-word entities
        self._known_phrases = ["google chrome", "visual studio code", "stack overflow"]

    def update_aliases(self, aliases: dict[str, str]) -> None:
        self._aliases.update(aliases)

    def normalize(self, text: str) -> NormalizedResult | str:
        """
        Returns a NormalizedResult (which behaves like a string for legacy code
        via __str__, but contains extracted metadata).
        """
        text = str(text).strip()
        if not text or len(text) < 2:
            return NormalizedResult(text="")

        metadata = {}

        # 1. lowercase
        text = text.lower()

        # 2. extract wake word (metadata preservation)
        wake_match = self._wake_pattern.search(text)
        if wake_match:
            metadata["wake_word"] = wake_match.group(1).strip()
            text = text[wake_match.end():].strip()

        # 3. ASR fixes (safe word boundaries)
        for bad, good in self._asr_fixes.items():
            text = re.sub(rf'\b{bad}\b', good, text)

        # 4. protect known phrases (before punct removal)
        for phrase in self._known_phrases:
            if phrase in text:
                protected = phrase.replace(" ", "_")
                text = text.replace(phrase, protected)

        # 5. remove punctuation
        text = re.sub(r'[^\w\s_]', '', text)

        # 6. remove fillers (only at start)
        text = text.strip()
        text = self._filler_pattern.sub('', text).strip()

        # 7. collapse spaces
        text = " ".join(text.split())

        # 8. alias + safe fuzzy + dedupe
        words = text.split()
        expanded = []

        valid_aliases = list(self._aliases.keys())
        
        for w in words:
            # Revert protected phrases
            if "_" in w and w.replace("_", " ") in self._known_phrases:
                expanded.extend(w.split("_"))
                continue

            if w in self._aliases:
                w = self._aliases[w]
            elif self._aliases and len(w) >= 4:
                # Limit fuzzy matching to words >= 4 chars to prevent "note" -> "not"
                match = difflib.get_close_matches(w, valid_aliases, n=1, cutoff=0.65)
                if match:
                    w = self._aliases[match[0]]
            
            # Safe dedupe (only dedupe specific tokens, not everything)
            if not expanded or expanded[-1] != w or w not in self._dedupe_tokens:
                expanded.append(w)

        final_text = " ".join(expanded)
        return NormalizedResult(text=final_text, metadata=metadata)
