from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CacheEntry:
    action_policy: Any
    timestamp: float
    ttl_seconds: float

    @property
    def is_expired(self) -> bool:
        return (time.monotonic() - self.timestamp) > self.ttl_seconds


class IntentCache:
    def __init__(self, default_ttl_seconds: float = 3600.0) -> None:
        self._cache: dict[str, CacheEntry] = {}
        self._default_ttl = default_ttl_seconds

    def get(self, normalized_text: str) -> Any | None:
        if not normalized_text:
            return None
        entry = self._cache.get(normalized_text)
        if entry is None:
            return None
        if entry.is_expired:
            del self._cache[normalized_text]
            return None
        return entry.action_policy

    def set(self, normalized_text: str, action_policy: Any, ttl_seconds: float | None = None) -> None:
        if not normalized_text:
            return
        self._cache[normalized_text] = CacheEntry(
            action_policy=action_policy,
            timestamp=time.monotonic(),
            ttl_seconds=ttl_seconds or self._default_ttl
        )
        
    def clear(self) -> None:
        self._cache.clear()

    def remove(self, normalized_text: str) -> None:
        self._cache.pop(normalized_text, None)
