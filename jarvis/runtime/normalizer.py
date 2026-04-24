from __future__ import annotations

import re
import difflib


class InputNormalizer:
    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._aliases = aliases or {}

        # Predefined ASR garbage replacements
        self._asr_fixes = {
            "jardubus": "jarvis",
            "woping": "open",
            "yutube": "youtube",
            "gogle": "google",
            "crome": "chrome",
        }

        # Remove common filler words that don't add semantic value
        self._filler_pattern = re.compile(
            r'\b(please|can you|could you|would you|just|kindly|go ahead and|hey|hi|hello|jarvis)\b',
            re.IGNORECASE
        )

    def update_aliases(self, aliases: dict[str, str]) -> None:
        self._aliases.update(aliases)

    def normalize(self, text: str) -> str:
        text = str(text).strip()
        if not text:
            return ""

        # 1. lowercase
        text = text.lower()

        # 2. ASR fixes (safe word boundaries)
        for bad, good in self._asr_fixes.items():
            text = re.sub(rf'\b{bad}\b', good, text)

        # 3. remove fillers
        text = self._filler_pattern.sub('', text)

        # 4. remove punctuation
        text = re.sub(r'[^\w\s]', '', text)

        # 5. collapse spaces
        text = " ".join(text.split())

        # 6. alias + fuzzy
        if self._aliases:
            words = text.split()
            expanded = []

            for w in words:
                if w in self._aliases:
                    expanded.append(self._aliases[w])
                else:
                    match = difflib.get_close_matches(
                        w, self._aliases.keys(), n=1, cutoff=0.65
                    )
                    expanded.append(self._aliases[match[0]] if match else w)

            # 7. dedupe consecutive words
            result = []
            for w in expanded:
                if not result or result[-1] != w:
                    result.append(w)

            text = " ".join(result)

        return text
