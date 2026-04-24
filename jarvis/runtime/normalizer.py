from __future__ import annotations

import re


class InputNormalizer:
    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._aliases = aliases or {}
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
        
        # Simple alias expansion (word-level)
        if self._aliases:
            words = text.split()
            expanded_words = [self._aliases.get(w, w) for w in words]
            text = " ".join(expanded_words)
            
        return text
