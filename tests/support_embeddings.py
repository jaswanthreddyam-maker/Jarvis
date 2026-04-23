from __future__ import annotations

import math
import re


class FakeEmbeddingProvider:
    def __init__(self, dimensions: int = 384) -> None:
        self._dimensions = max(len(self._TERMS) + 1, dimensions)

    _TERMS = (
        "lofi",
        "music",
        "dark",
        "mode",
        "chrome",
        "python",
        "study",
        "background",
        "favorite",
        "browser",
    )
    _TERM_INDEX = {term: index for index, term in enumerate(_TERMS)}

    @property
    def is_available(self) -> bool:
        return True

    @property
    def backend_name(self) -> str:
        return "test_embeddings"

    def embed_text(self, text: str) -> list[float]:
        tokens = re.findall(r"[a-z0-9]+", str(text or "").lower())
        vector = [0.0] * self._dimensions
        for token in tokens:
            index = self._TERM_INDEX.get(token)
            if index is not None:
                vector[index] += 1.0
        if not any(vector):
            vector[-1] = max(1.0, len(tokens) or 1.0)
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]
