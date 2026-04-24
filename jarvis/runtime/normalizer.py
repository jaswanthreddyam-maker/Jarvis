from __future__ import annotations

import re
import difflib
from dataclasses import dataclass, field


@dataclass(slots=True)
class NormalizedResult:
    text: str
    metadata: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.text


class InputNormalizer:
    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._aliases = aliases or {}

        self._phrase_aliases = {
            "vs code": "visual studio code",
            "yt": "youtube",
            "yt music": "youtube music",
        }

        self._asr_fixes = {
            "jardubus": "jarvis",
            "woping": "open",
            "yutube": "youtube",
            "gogle": "google",
            "crome": "chrome",
        }

        self._wake_pattern = re.compile(r'^(?:hey\s+|hi\s+)?jarvis[\s,]*', re.IGNORECASE)

        self._filler_pattern = re.compile(
            r'^(please|can you|could you|would you|just|kindly|go ahead and|hey|hi|hello)\b\s*',
            re.IGNORECASE
        )

        self._dedupe_tokens = {"open", "play", "go", "stop", "do", "search", "show"}

    def update_aliases(self, aliases: dict[str, str]) -> None:
        self._aliases.update(aliases)

    def _remove_fillers(self, text: str) -> str:
        while True:
            new = self._filler_pattern.sub('', text).strip()
            if new == text:
                return text
            text = new

    def normalize(self, text: str) -> NormalizedResult:
        text = str(text).strip()

        if not text:
            return NormalizedResult(text="")

        metadata = {
            "original": text,
            "fuzzy_used": False,
            "alias_used": False,
            "token_map": [],
        }

        text = text.lower()

        # Wake word
        match = self._wake_pattern.match(text)
        if match:
            metadata["wake_word"] = "jarvis"
            text = text[match.end():].strip()

        # ASR fixes
        for bad, good in self._asr_fixes.items():
            text = re.sub(rf'\b{bad}\b', good, text)

        # Phrase aliases (CRITICAL)
        for phrase, repl in self._phrase_aliases.items():
            text = re.sub(rf'\b{re.escape(phrase)}\b', repl, text)

        # Safe punctuation removal (allow path/URL chars)
        text = re.sub(r'[^\w\s_\-.:/]', '', text)

        text = self._remove_fillers(text)
        text = " ".join(text.split())

        words = text.split()
        expanded = []

        keys = list(self._aliases.keys())

        for w in words:
            original = w

            # direct alias
            if w in self._aliases:
                w = self._aliases[w]
                metadata["alias_used"] = True

            # controlled fuzzy
            elif self._aliases and len(w) >= 4:
                match = difflib.get_close_matches(w, keys, n=1, cutoff=0.8)
                if match and abs(len(w) - len(match[0])) <= 2:
                    w = self._aliases[match[0]]
                    metadata["fuzzy_used"] = True

            metadata["token_map"].append((original, w))

            if not expanded or expanded[-1] != w or w not in self._dedupe_tokens:
                expanded.append(w)

        final = " ".join(expanded)

        # Safety flag for pipeline
        if metadata["fuzzy_used"]:
            metadata["unsafe_normalization"] = True

        return NormalizedResult(text=final, metadata=metadata)
