from __future__ import annotations

import re


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
            r'\b(please|can you|could you|would you|just|kindly|go ahead and)\b', 
            re.IGNORECASE
        )

    def update_aliases(self, aliases: dict[str, str]) -> None:
        self._aliases.update(aliases)

    def normalize(self, text: str) -> str:
        text = str(text).strip()
        if not text:
            return ""
            
        # Remove fillers
        text = self._filler_pattern.sub('', text)
        
        # Lowercase
        text = text.lower()
        
        # Collapse multiple spaces
        text = " ".join(text.split())
        
        # Global string replacements for known mishears
        for bad_word, good_word in self._asr_fixes.items():
            text = text.replace(bad_word, good_word)

        # Simple alias expansion + fuzzy matching (word-level)
        if self._aliases:
            import difflib
            words = text.split()
            expanded_words = []
            valid_aliases = list(self._aliases.keys())
            for w in words:
                if w in self._aliases:
                    expanded_words.append(self._aliases[w])
                else:
                    # Fuzzy match fallback
                    matches = difflib.get_close_matches(w, valid_aliases, n=1, cutoff=0.8)
                    if matches:
                        expanded_words.append(self._aliases[matches[0]])
                    else:
                        expanded_words.append(w)
            text = " ".join(expanded_words)
            
        return text
