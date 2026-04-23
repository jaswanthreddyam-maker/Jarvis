from __future__ import annotations

import re


_SENTENCE_PATTERN = re.compile(r"(?<=[.!?])\s+")


def chunk_response_text(text: str, *, max_chars: int = 80) -> list[str]:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return []

    sentences = [item.strip() for item in _SENTENCE_PATTERN.split(cleaned) if item.strip()]
    if not sentences:
        return [cleaned]

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = sentence if not current else f"{current} {sentence}"
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
            continue
        current = candidate
    if current:
        chunks.append(current)
    return chunks or [cleaned]
