from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from jarvis.core.memory.long_term import LongTermMemory
from jarvis.core.memory.semantic import SemanticMemory
from jarvis.core.memory.short_term import ShortTermMemory


@dataclass(slots=True)
class MemoryContextSnapshot:
    short_term: list[dict[str, Any]] = field(default_factory=list)
    long_term: dict[str, Any] = field(default_factory=dict)
    semantic: list[dict[str, Any]] = field(default_factory=list)
    write_policy: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "short_term": list(self.short_term),
            "long_term": dict(self.long_term),
            "semantic": list(self.semantic),
            "write_policy": dict(self.write_policy),
        }


class MemoryManager:
    """Coordinates short-term, long-term, and semantic memory with write policy."""

    _SENSITIVE_PATTERNS = (
        re.compile(r"\b(password|passcode|otp|one time password|secret|api key|access token|auth token)\b", re.IGNORECASE),
        re.compile(r"(?<![\d.])(?:\d{4}[- ]?){3}\d{4}\b"),
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    )
    _SEMANTIC_CATEGORIES = frozenset({"fact", "preference", "profile", "automation", "memory"})

    def __init__(
        self,
        *,
        short_term: ShortTermMemory,
        long_term: LongTermMemory,
        semantic: SemanticMemory,
    ) -> None:
        self._short_term = short_term
        self._long_term = long_term
        self._semantic = semantic

    def add_interaction(
        self,
        user: str,
        assistant: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._short_term.add_interaction(user, assistant, metadata=metadata)

    def save_preference(
        self,
        key: str,
        value: Any,
        *,
        source: str = "explicit",
        confidence: float = 1.0,
    ) -> dict[str, Any] | None:
        if self._is_sensitive(f"{key}: {value}"):
            return None
        entry = self._long_term.save_preference(key, value, source=source, confidence=confidence)
        preference_text = f"Preference {entry['key']} = {entry['value']}"
        self._long_term.remember(
            namespace="system",
            content=preference_text,
            category="preference",
            metadata={"source": source, "confidence": confidence},
        )
        self._semantic.store_memory(
            preference_text,
            metadata={"category": "preference", "key": entry["key"]},
        )
        return entry

    def get_preference(self, key: str, default: Any | None = None) -> Any:
        return self._long_term.get_preference(key, default)

    def delete_preference(self, key: str) -> bool:
        return self._long_term.delete_preference(key)

    def preferences(self) -> dict[str, Any]:
        return self._long_term.preferences()

    def relevant_preferences(self, query: str, *, limit: int = 4) -> dict[str, Any]:
        return self._long_term.relevant_preferences(query, limit=limit)

    def remember(
        self,
        namespace: str,
        content: str,
        category: str = "fact",
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        cleaned = content.strip()
        if not cleaned or self._is_sensitive(cleaned):
            return False
        self._long_term.remember(namespace, cleaned, category, metadata=metadata)
        if category.strip().lower() in self._SEMANTIC_CATEGORIES:
            self._semantic.store_memory(
                cleaned,
                metadata={
                    "namespace": namespace,
                    "category": category,
                    **dict(metadata or {}),
                },
            )
        return True

    def recall(self, query: str, *, namespace: str = "system", limit: int = 5) -> list[dict[str, Any]]:
        records = self._long_term.recall(query, namespace=namespace, limit=limit)
        if records or not query.strip() or namespace not in {"system", "user_profile", "preferences"}:
            return records
        semantic_matches = self.retrieve_similar(query, limit=limit)
        return [
            {
                "id": item["id"],
                "timestamp": item["created_at"],
                "namespace": namespace,
                "category": "semantic",
                "content": item["text"],
                "metadata": dict(item.get("metadata", {})),
                "score": item["score"],
            }
            for item in semantic_matches
        ]

    def store_memory(self, text: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        cleaned = text.strip()
        if not cleaned or self._is_sensitive(cleaned):
            return None
        return self._semantic.store_memory(cleaned, metadata=metadata)

    def retrieve_similar(self, query: str, *, limit: int = 4) -> list[dict[str, Any]]:
        return self._semantic.retrieve_similar(query, limit=limit)

    def recent_conversation(self, *, limit: int = 5) -> list[dict[str, Any]]:
        return self._short_term.recent(limit=limit)

    def build_context(
        self,
        query: str,
        *,
        short_term_limit: int = 6,
        long_term_limit: int = 4,
        semantic_limit: int = 4,
    ) -> MemoryContextSnapshot:
        relevant_facts = self._long_term.recall(query, namespace="system", limit=long_term_limit)
        preferences = self.relevant_preferences(query, limit=long_term_limit)
        if not preferences:
            preferences = self.preferences()
        return MemoryContextSnapshot(
            short_term=self.recent_conversation(limit=short_term_limit),
            long_term={
                "preferences": preferences,
                "facts": relevant_facts,
            },
            semantic=self.retrieve_similar(query, limit=semantic_limit),
            write_policy=self.write_policy(),
        )

    @staticmethod
    def write_policy() -> dict[str, Any]:
        return {
            "store_only": [
                "preferences",
                "important_facts",
                "repeated_patterns",
            ],
            "avoid": [
                "random_chitchat",
                "temporary_noise",
                "sensitive_secrets",
            ],
        }

    def clear_short_term(self) -> None:
        self._short_term.clear()

    def reset_all(self) -> None:
        self._short_term.clear()
        self._long_term.reset()
        self._semantic.reset()

    def _is_sensitive(self, text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            return False
        return any(pattern.search(normalized) for pattern in self._SENSITIVE_PATTERNS)
