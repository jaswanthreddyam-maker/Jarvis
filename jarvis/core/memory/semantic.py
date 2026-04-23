from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

import httpx

from jarvis.config.settings import load_settings

try:  # pragma: no cover - optional runtime dependency
    import chromadb
except ImportError:  # pragma: no cover
    chromadb = None  # type: ignore[assignment]

try:  # pragma: no cover - optional runtime dependency
    from sentence_transformers import SentenceTransformer
except ImportError:  # pragma: no cover
    SentenceTransformer = None  # type: ignore[assignment]


logger = logging.getLogger("Jarvis.SemanticMemory")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    return " ".join(str(text).strip().lower().split())


class SemanticMemory:
    """Semantic memory backed by real embeddings."""

    def __init__(
        self,
        storage_path: Path | str,
        *,
        dimensions: int = 384,
        max_entries: int = 512,
        embedding_provider: "_EmbeddingProvider | None" = None,
        require_embeddings: bool = True,
    ) -> None:
        self._storage_path = Path(storage_path)
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._dimensions = max(64, dimensions)
        self._max_entries = max(16, max_entries)
        self._lock = threading.RLock()
        self._settings = load_settings()
        self._embedder = embedding_provider or _EmbeddingProvider(dimensions=self._dimensions, settings=self._settings)
        if require_embeddings and not self._embedder.is_available:
            raise RuntimeError("Semantic memory requires real embeddings")
        self._mode = "json_store"
        self._payload = {"entries": []}
        self._client = None
        self._collection = None

        if chromadb is not None:
            try:
                storage_dir = self._storage_path.with_suffix(".chromadb")
                storage_dir.mkdir(parents=True, exist_ok=True)
                collection_name = self._storage_path.stem.replace(".", "_").replace("-", "_")
                self._client = chromadb.PersistentClient(path=str(storage_dir))
                self._collection = self._client.get_or_create_collection(
                    name=collection_name,
                    metadata={"hnsw:space": "cosine"},
                )
                self._mode = "chromadb"
            except Exception as exc:  # pragma: no cover - depends on local env
                logger.warning("ChromaDB initialization failed; using JSON semantic store: %s", exc)
                self._collection = None

        if self._collection is None:
            self._payload = self._load_store()

    @property
    def embedding_backend(self) -> str:
        return self._embedder.backend_name

    def store_memory(
        self,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = _normalize(text)
        if not normalized:
            raise ValueError("Semantic memory text must not be empty.")

        entry = {
            "id": uuid4().hex,
            "text": text.strip(),
            "normalized_text": normalized,
            "metadata": dict(metadata or {}),
            "created_at": _now_iso(),
            "last_accessed_at": _now_iso(),
            "access_count": 0,
            "backend": self._mode,
        }
        embedding = self._embedder.embed_text(normalized)

        if self._collection is not None:
            with self._lock:
                existing = self._collection.get(where={"normalized_text": normalized})
                if existing and existing.get("ids"):
                    entry["id"] = str(existing["ids"][0])
                self._collection.upsert(
                    ids=[entry["id"]],
                    documents=[entry["text"]],
                    embeddings=[embedding],
                    metadatas=[{**entry["metadata"], "normalized_text": normalized, "created_at": entry["created_at"]}],
                )
                self._prune_collection_locked()
            return dict(entry)

        with self._lock:
            entries = list(self._payload.get("entries", []))
            for existing in entries:
                if str(existing.get("normalized_text", "")) == normalized:
                    existing["metadata"] = dict(existing.get("metadata", {})) | dict(metadata or {})
                    existing["last_accessed_at"] = _now_iso()
                    existing["text"] = text.strip()
                    existing["embedding"] = embedding
                    existing["backend"] = self._mode
                    self._payload["entries"] = self._prune_entries(entries)
                    self._save_store_locked()
                    return dict(existing)
            entry["embedding"] = embedding
            entries.append(entry)
            self._payload["entries"] = self._prune_entries(entries)
            self._save_store_locked()
        return dict(entry)

    def retrieve_similar(
        self,
        query: str,
        *,
        limit: int = 4,
        min_score: float = 0.18,
    ) -> list[dict[str, Any]]:
        normalized = _normalize(query)
        if not normalized:
            return []
        query_vector = self._embedder.embed_text(normalized)

        if self._collection is not None:
            with self._lock:
                result = self._collection.query(
                    query_embeddings=[query_vector],
                    n_results=max(1, limit),
                    include=["documents", "metadatas", "distances"],
                )
            documents = (result.get("documents") or [[]])[0]
            metadatas = (result.get("metadatas") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            ids = (result.get("ids") or [[]])[0]
            matches: list[dict[str, Any]] = []
            for index, document in enumerate(documents):
                distance = float(distances[index] or 0.0) if index < len(distances) else 0.0
                score = max(0.0, 1.0 - distance)
                if score < min_score:
                    continue
                metadata = dict(metadatas[index] or {}) if index < len(metadatas) else {}
                matches.append(
                    {
                        "id": str(ids[index]) if index < len(ids) else uuid4().hex,
                        "text": str(document or ""),
                        "metadata": {key: value for key, value in metadata.items() if key not in {"normalized_text", "created_at"}},
                        "created_at": str(metadata.get("created_at", _now_iso())),
                        "last_accessed_at": _now_iso(),
                        "access_count": 1,
                        "score": round(score, 4),
                        "backend": self._mode,
                    }
                )
            return matches

        with self._lock:
            entries = list(self._payload.get("entries", []))
        ranked: list[tuple[float, dict[str, Any]]] = []
        for entry in entries:
            score = self._cosine_similarity(query_vector, list(entry.get("embedding", [])))
            if score < min_score:
                continue
            item = dict(entry)
            item["score"] = round(score, 4)
            ranked.append((score, item))
        ranked.sort(key=lambda item: (item[0], item[1].get("last_accessed_at", "")), reverse=True)
        top_results = [item for _, item in ranked[: max(0, limit)]]
        if top_results:
            self._mark_accessed([str(item.get("id", "")) for item in top_results])
        return top_results

    def reset(self) -> None:
        if self._collection is not None:
            with self._lock:
                try:
                    self._client.delete_collection(self._collection.name)
                except Exception:
                    pass
                self._collection = self._client.get_or_create_collection(
                    name=self._storage_path.stem.replace(".", "_").replace("-", "_"),
                    metadata={"hnsw:space": "cosine"},
                )
            return
        with self._lock:
            self._payload = {"entries": []}
            self._save_store_locked()

    def _mark_accessed(self, entry_ids: list[str]) -> None:
        if not entry_ids or self._collection is not None:
            return
        with self._lock:
            dirty = False
            for entry in self._payload.get("entries", []):
                if str(entry.get("id", "")) not in entry_ids:
                    continue
                entry["last_accessed_at"] = _now_iso()
                entry["access_count"] = int(entry.get("access_count", 0)) + 1
                dirty = True
            if dirty:
                self._save_store_locked()

    def _load_store(self) -> dict[str, Any]:
        if not self._storage_path.exists():
            return {"entries": []}
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except Exception:
            return {"entries": []}
        if not isinstance(payload, dict):
            return {"entries": []}
        payload.setdefault("entries", [])
        return payload

    def _save_store_locked(self) -> None:
        self._storage_path.write_text(json.dumps(self._payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _prune_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(entries) <= self._max_entries:
            return entries
        ranked = sorted(
            entries,
            key=lambda item: (
                int(item.get("access_count", 0)),
                str(item.get("last_accessed_at", "")),
                str(item.get("created_at", "")),
            ),
            reverse=True,
        )
        return ranked[: self._max_entries]

    def _prune_collection_locked(self) -> None:
        if self._collection is None:
            return
        try:
            payload = self._collection.get(include=["metadatas"])
        except Exception:
            return
        ids = list(payload.get("ids") or [])
        metadatas = list(payload.get("metadatas") or [])
        if len(ids) <= self._max_entries:
            return
        ranked = sorted(
            zip(ids, metadatas),
            key=lambda item: (
                int((item[1] or {}).get("access_count", 0) or 0),
                str((item[1] or {}).get("last_accessed_at", "")),
                str((item[1] or {}).get("created_at", "")),
            ),
        )
        overflow = [item_id for item_id, _ in ranked[: len(ids) - self._max_entries]]
        if overflow:
            self._collection.delete(ids=overflow)

    @staticmethod
    def _cosine_similarity(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        numerator = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return numerator / (left_norm * right_norm)


class _EmbeddingProvider:
    _shared_models: dict[str, Any] = {}
    _shared_failures: set[str] = set()

    def __init__(self, *, dimensions: int, settings) -> None:
        self._dimensions = dimensions
        self._settings = settings
        self._model_name = str(getattr(settings, "models", {}).get("semantic_embedding", "all-MiniLM-L6-v2") or "all-MiniLM-L6-v2")
        self._model = self._resolve_model(
            model_name=self._model_name,
            local_only=bool(getattr(settings, "offline_mode", False)),
        )
        self._backend_name = ""
        if self._model is not None:
            self._backend_name = "local_sentence_transformer"
        elif self._settings.providers.openai_api_key:
            self._backend_name = "openai_api"

    @property
    def is_available(self) -> bool:
        return bool(self._model is not None or self._settings.providers.openai_api_key)

    @property
    def backend_name(self) -> str:
        return self._backend_name or "unavailable"

    @classmethod
    def _resolve_model(cls, *, model_name: str, local_only: bool):
        cache_key = f"{model_name}|{int(local_only)}"
        if cache_key in cls._shared_models:
            return cls._shared_models[cache_key]
        if SentenceTransformer is None or cache_key in cls._shared_failures:  # pragma: no cover - depends on local env
            cls._shared_models[cache_key] = None
            return cls._shared_models[cache_key]
        try:  # pragma: no cover - depends on local env
            cls._shared_models[cache_key] = SentenceTransformer(model_name, local_files_only=local_only)
        except Exception as exc:
            logger.warning("SentenceTransformer unavailable for semantic memory: %s", exc)
            cls._shared_models[cache_key] = None
            cls._shared_failures.add(cache_key)
        return cls._shared_models[cache_key]

    def embed_text(self, text: str) -> list[float]:
        if self._model is not None:  # pragma: no cover - depends on local env
            vector = self._model.encode([text], normalize_embeddings=True)[0]
            return [float(item) for item in vector]
        if self._settings.providers.openai_api_key:
            return self._embed_with_openai(text)
        raise RuntimeError("Semantic memory requires real embeddings")

    def _embed_with_openai(self, text: str) -> list[float]:
        try:
            with httpx.Client(timeout=self._settings.providers.request_timeout_seconds) as client:
                response = client.post(
                    self._settings.providers.openai_base_url.rstrip("/") + "/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._settings.providers.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": "text-embedding-3-small", "input": text},
                )
                response.raise_for_status()
                payload = response.json()
            data = list(payload.get("data") or [])
            if not data:
                raise RuntimeError("OpenAI embeddings response did not contain any vectors.")
            return [float(item) for item in list(data[0].get("embedding") or [])]
        except Exception as exc:  # pragma: no cover - depends on env/config
            logger.warning("OpenAI embeddings unavailable for semantic memory: %s", exc)
            raise RuntimeError("Semantic memory requires real embeddings") from exc
