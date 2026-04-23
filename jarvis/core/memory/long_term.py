from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    return " ".join(str(text).strip().lower().split())


def _tokens(text: str) -> set[str]:
    return {token for token in _normalize(text).replace(":", " ").replace("-", " ").split(" ") if token}


class LongTermMemory:
    """Persistent structured storage for preferences and important facts."""

    def __init__(
        self,
        storage_path: Path | str,
        *,
        max_facts: int = 512,
    ) -> None:
        self._storage_path = Path(storage_path)
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._max_facts = max(8, max_facts)
        self._lock = threading.RLock()
        self._payload = self._load()

    def save_preference(
        self,
        key: str,
        value: Any,
        *,
        source: str = "explicit",
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        normalized_key = _normalize(key).replace(" ", "_")
        if not normalized_key:
            raise ValueError("Preference key must not be empty.")
        entry = {
            "value": value,
            "updated_at": _now_iso(),
            "source": source.strip() or "explicit",
            "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
        }
        with self._lock:
            self._payload["preferences"][normalized_key] = entry
            self._save_locked()
        return {"key": normalized_key, **entry}

    def get_preference(self, key: str, default: Any | None = None) -> Any:
        normalized_key = _normalize(key).replace(" ", "_")
        with self._lock:
            entry = dict(self._payload.get("preferences", {}).get(normalized_key) or {})
        if not entry:
            return default
        return entry.get("value", default)

    def preferences(self) -> dict[str, Any]:
        with self._lock:
            return {
                key: value.get("value")
                for key, value in dict(self._payload.get("preferences", {})).items()
            }

    def preference_entries(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                key: dict(value)
                for key, value in dict(self._payload.get("preferences", {})).items()
            }

    def delete_preference(self, key: str) -> bool:
        normalized_key = _normalize(key).replace(" ", "_")
        with self._lock:
            if normalized_key not in self._payload.get("preferences", {}):
                return False
            self._payload["preferences"].pop(normalized_key, None)
            self._save_locked()
        return True

    def remember(
        self,
        namespace: str,
        content: str,
        category: str = "fact",
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": uuid4().hex,
            "timestamp": _now_iso(),
            "namespace": namespace.strip() or "system",
            "category": category.strip() or "fact",
            "content": content.strip(),
            "metadata": dict(metadata or {}),
        }
        with self._lock:
            facts = list(self._payload.get("facts", []))
            facts.append(entry)
            self._payload["facts"] = facts[-self._max_facts :]
            self._save_locked()
        return dict(entry)

    def recall(
        self,
        query: str,
        *,
        namespace: str = "system",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        normalized_query = _normalize(query)
        query_tokens = _tokens(normalized_query)
        with self._lock:
            facts = list(self._payload.get("facts", []))

        namespaced = [
            dict(entry)
            for entry in facts
            if str(entry.get("namespace", "")).strip() == namespace
        ]
        if not normalized_query:
            return list(reversed(namespaced[-max(0, limit) :]))

        ranked: list[tuple[float, dict[str, Any]]] = []
        for entry in namespaced:
            score = self._score_text(
                normalized_query,
                query_tokens,
                str(entry.get("content", "")),
            )
            if score <= 0.0:
                continue
            ranked.append((score, entry))
        ranked.sort(key=lambda item: (item[0], item[1].get("timestamp", "")), reverse=True)
        return [entry for _, entry in ranked[: max(0, limit)]]

    def relevant_preferences(self, query: str, *, limit: int = 4) -> dict[str, Any]:
        normalized_query = _normalize(query)
        query_tokens = _tokens(normalized_query)
        with self._lock:
            preferences = dict(self._payload.get("preferences", {}))
        if not preferences:
            return {}
        if not normalized_query:
            items = sorted(
                preferences.items(),
                key=lambda item: str(item[1].get("updated_at", "")),
                reverse=True,
            )
            return {key: value.get("value") for key, value in items[: max(0, limit)]}

        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for key, entry in preferences.items():
            value_text = str(entry.get("value", ""))
            score = self._score_text(
                normalized_query,
                query_tokens,
                f"{key} {value_text}",
            )
            if score <= 0.0:
                continue
            ranked.append((score, key, dict(entry)))
        ranked.sort(key=lambda item: (item[0], str(item[2].get("updated_at", ""))), reverse=True)
        return {
            key: entry.get("value")
            for _, key, entry in ranked[: max(0, limit)]
        }

    def reset(self) -> None:
        with self._lock:
            self._payload = self._default_payload()
            self._save_locked()

    def _load(self) -> dict[str, Any]:
        if not self._storage_path.exists():
            return self._default_payload()
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except Exception:
            return self._default_payload()
        if not isinstance(payload, dict):
            return self._default_payload()
        payload.setdefault("preferences", {})
        payload.setdefault("facts", [])
        return payload

    def _save_locked(self) -> None:
        self._storage_path.write_text(
            json.dumps(self._payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _default_payload() -> dict[str, Any]:
        return {"preferences": {}, "facts": []}

    @staticmethod
    def _score_text(normalized_query: str, query_tokens: set[str], candidate: str) -> float:
        normalized_candidate = _normalize(candidate)
        if not normalized_candidate:
            return 0.0
        score = 0.0
        if normalized_query and normalized_query in normalized_candidate:
            score += 1.2
        candidate_tokens = _tokens(normalized_candidate)
        if query_tokens and candidate_tokens:
            overlap = len(query_tokens & candidate_tokens) / max(1, len(query_tokens))
            score += overlap
        return score
