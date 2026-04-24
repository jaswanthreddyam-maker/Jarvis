"""
Companion Memory System — first-class memory with working, episodic, and semantic layers.

Memory is NOT just chat logs. It's a scored, decaying, retrievable knowledge graph about
the user that drives companion-like behavior across sessions.

Layers:
  - WorkingMemory: current session context (turns, goals, threads)
  - EpisodicMemory: meaningful past events scored by importance
  - SemanticFacts: stable user facts, preferences, relationships
  - MemoryConsolidator: post-session extraction & scoring
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger("Companion.Memory")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


# ── Working Memory (session-scoped) ──────────────────────────────────────────

@dataclass(slots=True)
class ConversationTurn:
    role: str           # "user" or "assistant"
    content: str
    timestamp: str = field(default_factory=_now_iso)
    emotion: str = "neutral"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActiveThread:
    """An unresolved conversational thread the companion should track."""
    topic: str
    created_at: str = field(default_factory=_now_iso)
    resolved: bool = False
    turns_since_mentioned: int = 0


class WorkingMemory:
    """
    In-session volatile memory. Holds recent turns, active goals, unresolved threads,
    and current emotional trajectory. Cleared between sessions but consolidated into
    episodic memory on session end.
    """

    def __init__(self, *, max_turns: int = 20) -> None:
        self._turns: deque[ConversationTurn] = deque(maxlen=max(4, max_turns))
        self._active_threads: list[ActiveThread] = []
        self._session_goals: list[str] = []
        self._emotion_trajectory: list[tuple[str, str]] = []  # (timestamp, emotion)
        self._session_start = _now_iso()
        self._lock = threading.RLock()

    def add_turn(self, role: str, content: str, *, emotion: str = "neutral", **meta: Any) -> None:
        turn = ConversationTurn(role=role, content=content, emotion=emotion, metadata=dict(meta))
        with self._lock:
            self._turns.append(turn)
            if role == "user":
                self._emotion_trajectory.append((_now_iso(), emotion))
                # Age unresolved threads
                for thread in self._active_threads:
                    if not thread.resolved:
                        thread.turns_since_mentioned += 1

    def add_thread(self, topic: str) -> None:
        with self._lock:
            self._active_threads.append(ActiveThread(topic=topic))

    def resolve_thread(self, topic: str) -> None:
        with self._lock:
            for thread in self._active_threads:
                if thread.topic.lower() == topic.lower():
                    thread.resolved = True

    def add_goal(self, goal: str) -> None:
        with self._lock:
            if goal not in self._session_goals:
                self._session_goals.append(goal)

    def recent_turns(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            turns = list(self._turns)
        if limit:
            turns = turns[-limit:]
        return [
            {"role": t.role, "content": t.content, "timestamp": t.timestamp, "emotion": t.emotion}
            for t in turns
        ]

    def conversation_for_llm(self, limit: int = 10) -> list[dict[str, str]]:
        """Format for LLM message array."""
        with self._lock:
            turns = list(self._turns)
        if limit:
            turns = turns[-limit:]
        return [{"role": t.role, "content": t.content} for t in turns]

    def unresolved_threads(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"topic": t.topic, "created_at": t.created_at, "turns_since": t.turns_since_mentioned}
                for t in self._active_threads if not t.resolved
            ]

    def emotion_summary(self) -> dict[str, Any]:
        with self._lock:
            trajectory = list(self._emotion_trajectory)
        if not trajectory:
            return {"dominant": "neutral", "shifts": 0, "trajectory": []}
        emotions = [e for _, e in trajectory]
        from collections import Counter
        counts = Counter(emotions)
        dominant = counts.most_common(1)[0][0]
        shifts = sum(1 for i in range(1, len(emotions)) if emotions[i] != emotions[i - 1])
        return {"dominant": dominant, "shifts": shifts, "trajectory": trajectory[-5:]}

    def snapshot(self) -> dict[str, Any]:
        return {
            "recent_turns": self.recent_turns(8),
            "unresolved_threads": self.unresolved_threads(),
            "goals": list(self._session_goals),
            "emotion": self.emotion_summary(),
            "session_start": self._session_start,
            "turn_count": len(self._turns),
        }

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()
            self._active_threads.clear()
            self._session_goals.clear()
            self._emotion_trajectory.clear()
            self._session_start = _now_iso()


# ── Episodic Memory (cross-session important events) ─────────────────────────

@dataclass(slots=True)
class EpisodicEntry:
    id: str
    summary: str
    emotion: str
    importance_score: float      # 0.0 to 1.0
    created_at: str
    last_accessed_at: str
    access_count: int
    decay_factor: float          # multiplied by importance over time
    tags: list[str]
    session_id: str
    raw_turns: list[dict[str, Any]]   # original conversation snippet
    metadata: dict[str, Any]


class EpisodicMemory:
    """
    Stores meaningful past events, emotionally significant moments, and
    recurring patterns. Entries are scored by importance and decay over time.
    """

    DECAY_RATE = 0.02            # importance decays by 2% per day
    MIN_IMPORTANCE = 0.05        # below this, memory is eligible for forgetting
    EMOTIONAL_BOOST = {
        "happy": 0.1, "excited": 0.15, "sad": 0.12, "frustrated": 0.1,
        "anxious": 0.08, "grateful": 0.12, "surprised": 0.1,
    }

    def __init__(self, storage_path: Path | str, *, max_episodes: int = 256) -> None:
        self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max = max(16, max_episodes)
        self._lock = threading.RLock()
        self._entries: list[EpisodicEntry] = self._load()

    def store(
        self,
        summary: str,
        *,
        emotion: str = "neutral",
        importance: float = 0.5,
        tags: list[str] | None = None,
        session_id: str = "",
        raw_turns: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EpisodicEntry:
        # Boost importance for emotional events
        boost = self.EMOTIONAL_BOOST.get(emotion, 0.0)
        final_importance = min(1.0, importance + boost)

        entry = EpisodicEntry(
            id=uuid4().hex,
            summary=summary.strip(),
            emotion=emotion,
            importance_score=final_importance,
            created_at=_now_iso(),
            last_accessed_at=_now_iso(),
            access_count=0,
            decay_factor=1.0,
            tags=list(tags or []),
            session_id=session_id,
            raw_turns=list(raw_turns or []),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._entries.append(entry)
            self._prune()
            self._save()
        return entry

    def recall(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Retrieve episodes relevant to query, ranked by effective importance."""
        query_lower = query.lower()
        query_tokens = set(query_lower.split())
        with self._lock:
            self._apply_decay()
            candidates = []
            for entry in self._entries:
                score = self._relevance_score(entry, query_lower, query_tokens)
                if score > 0.05:
                    candidates.append((score, entry))
        candidates.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, entry in candidates[:limit]:
            self._mark_accessed(entry.id)
            results.append({
                "id": entry.id,
                "summary": entry.summary,
                "emotion": entry.emotion,
                "importance": round(entry.importance_score * entry.decay_factor, 3),
                "tags": entry.tags,
                "created_at": entry.created_at,
                "relevance_score": round(score, 3),
            })
        return results

    def forget_stale(self) -> int:
        """Remove entries below minimum importance threshold."""
        with self._lock:
            self._apply_decay()
            before = len(self._entries)
            self._entries = [
                e for e in self._entries
                if (e.importance_score * e.decay_factor) >= self.MIN_IMPORTANCE
            ]
            forgotten = before - len(self._entries)
            if forgotten:
                self._save()
            return forgotten

    def update_importance(self, entry_id: str, delta: float) -> None:
        with self._lock:
            for entry in self._entries:
                if entry.id == entry_id:
                    entry.importance_score = max(0.0, min(1.0, entry.importance_score + delta))
                    self._save()
                    break

    def all_episodes(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            entries = sorted(self._entries, key=lambda e: e.created_at, reverse=True)
        return [
            {"id": e.id, "summary": e.summary, "emotion": e.emotion,
             "importance": round(e.importance_score * e.decay_factor, 3),
             "created_at": e.created_at, "tags": e.tags}
            for e in entries[:limit]
        ]

    def _relevance_score(self, entry: EpisodicEntry, query_lower: str, query_tokens: set[str]) -> float:
        effective_importance = entry.importance_score * entry.decay_factor
        text = f"{entry.summary} {' '.join(entry.tags)}".lower()
        text_tokens = set(text.split())

        # Token overlap
        overlap = len(query_tokens & text_tokens) / max(1, len(query_tokens)) if query_tokens else 0
        # Substring match bonus
        substr_bonus = 0.3 if query_lower in text else 0.0
        # Recency bonus (more recent = higher)
        try:
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(entry.created_at)).days
        except Exception:
            age_days = 30
        recency = 1.0 / (1.0 + age_days * 0.05)

        return (overlap * 0.4 + substr_bonus + recency * 0.15 + effective_importance * 0.15)

    def _apply_decay(self) -> None:
        now = _now_ts()
        for entry in self._entries:
            try:
                created = datetime.fromisoformat(entry.created_at).timestamp()
            except Exception:
                continue
            age_days = max(0, (now - created) / 86400)
            entry.decay_factor = math.exp(-self.DECAY_RATE * age_days)

    def _mark_accessed(self, entry_id: str) -> None:
        with self._lock:
            for entry in self._entries:
                if entry.id == entry_id:
                    entry.access_count += 1
                    entry.last_accessed_at = _now_iso()
                    # Accessing a memory boosts its importance slightly
                    entry.importance_score = min(1.0, entry.importance_score + 0.02)
                    self._save()
                    break

    def _prune(self) -> None:
        if len(self._entries) <= self._max:
            return
        self._apply_decay()
        self._entries.sort(key=lambda e: e.importance_score * e.decay_factor)
        self._entries = self._entries[-(self._max):]

    def _load(self) -> list[EpisodicEntry]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return [EpisodicEntry(**item) for item in data.get("episodes", [])]
        except Exception as exc:
            logger.warning("Failed to load episodic memory: %s", exc)
            return []

    def _save(self) -> None:
        data = {"episodes": [
            {
                "id": e.id, "summary": e.summary, "emotion": e.emotion,
                "importance_score": e.importance_score, "created_at": e.created_at,
                "last_accessed_at": e.last_accessed_at, "access_count": e.access_count,
                "decay_factor": e.decay_factor, "tags": e.tags,
                "session_id": e.session_id, "raw_turns": e.raw_turns,
                "metadata": e.metadata,
            }
            for e in self._entries
        ]}
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._save()


# ── Memory Consolidator (post-session extraction) ────────────────────────────

class MemoryConsolidator:
    """
    Runs after a session ends. Extracts important facts, emotional moments,
    and user preferences from the working memory and stores them in episodic
    and semantic layers.
    """

    # Simple heuristics for what's worth remembering
    IMPORTANCE_SIGNALS = {
        "user shared personal info": 0.8,
        "user expressed strong emotion": 0.7,
        "user stated a preference": 0.75,
        "user asked to remember something": 0.95,
        "recurring topic": 0.6,
        "user corrected the assistant": 0.65,
        "multi-turn deep conversation": 0.7,
    }

    def __init__(
        self,
        *,
        episodic: EpisodicMemory,
        llm_summarizer: Any = None,
    ) -> None:
        self._episodic = episodic
        self._llm = llm_summarizer  # Optional LLM for smart summarization

    def consolidate(
        self,
        working: WorkingMemory,
        *,
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        """Extract and store important memories from the completed session."""
        snapshot = working.snapshot()
        turns = snapshot.get("recent_turns", [])
        if len(turns) < 2:
            return []

        created = []
        emotion_data = snapshot.get("emotion", {})
        dominant_emotion = emotion_data.get("dominant", "neutral")

        # Rule 1: If session had strong emotions, store the emotional arc
        if emotion_data.get("shifts", 0) >= 2 or dominant_emotion not in ("neutral",):
            summary = self._summarize_session(turns, dominant_emotion)
            entry = self._episodic.store(
                summary,
                emotion=dominant_emotion,
                importance=0.6,
                tags=["emotional_session", dominant_emotion],
                session_id=session_id,
                raw_turns=turns[-6:],
            )
            created.append({"type": "emotional_arc", "id": entry.id, "summary": entry.summary})

        # Rule 2: If there were unresolved threads, store them
        for thread in snapshot.get("unresolved_threads", []):
            entry = self._episodic.store(
                f"Unresolved topic: {thread['topic']}",
                importance=0.5,
                tags=["unresolved", thread["topic"].lower()],
                session_id=session_id,
            )
            created.append({"type": "unresolved_thread", "id": entry.id, "topic": thread["topic"]})

        # Rule 3: Long sessions are more likely to contain important content
        if snapshot.get("turn_count", 0) >= 10:
            summary = self._summarize_session(turns, dominant_emotion)
            entry = self._episodic.store(
                f"Extended conversation: {summary}",
                importance=0.55,
                tags=["long_session"],
                session_id=session_id,
                raw_turns=turns[-4:],
            )
            created.append({"type": "long_session", "id": entry.id, "summary": entry.summary})

        logger.info("Consolidated %d memories from session %s", len(created), session_id)
        return created

    def _summarize_session(self, turns: list[dict[str, Any]], emotion: str) -> str:
        """Simple extractive summary. Use LLM if available for better quality."""
        if not turns:
            return "Empty session"
        user_messages = [t["content"] for t in turns if t.get("role") == "user"]
        if not user_messages:
            return "Session with no user input"
        # Take first and last user messages as bookends
        first = user_messages[0][:100]
        last = user_messages[-1][:100] if len(user_messages) > 1 else ""
        summary = f"User discussed: {first}"
        if last and last != first:
            summary += f" → ended with: {last}"
        if emotion != "neutral":
            summary += f" (mood: {emotion})"
        return summary


# ── Unified Memory Interface ─────────────────────────────────────────────────

class CompanionMemory:
    """
    Unified interface over all memory layers. This is what the voice pipeline
    and orchestrator interact with.
    """

    def __init__(
        self,
        *,
        data_dir: Path | str,
        existing_memory_manager: Any = None,
    ) -> None:
        data_path = Path(data_dir)
        data_path.mkdir(parents=True, exist_ok=True)

        self.working = WorkingMemory(max_turns=20)
        self.episodic = EpisodicMemory(data_path / "episodic_memory.json")
        self.consolidator = MemoryConsolidator(episodic=self.episodic)

        # Bridge to existing Jarvis memory system for semantic/long-term
        self._legacy_memory = existing_memory_manager

    def add_turn(self, role: str, content: str, **kwargs: Any) -> None:
        self.working.add_turn(role, content, **kwargs)
        # Also record in legacy system if available
        if self._legacy_memory and role == "user":
            pass  # Will be recorded when assistant responds

    def record_exchange(self, user_text: str, assistant_text: str, **meta: Any) -> None:
        """Record a complete user→assistant exchange."""
        self.working.add_turn("user", user_text, **meta)
        self.working.add_turn("assistant", assistant_text)
        if self._legacy_memory:
            self._legacy_memory.add_interaction(user_text, assistant_text, metadata=meta)

    def retrieve_for_response(self, query: str) -> dict[str, Any]:
        """
        Fetch all relevant memory context before generating a response.
        Returns a dict suitable for injection into the LLM prompt.
        """
        context: dict[str, Any] = {}

        # Working memory — always included
        context["recent_turns"] = self.working.recent_turns(8)
        context["unresolved_threads"] = self.working.unresolved_threads()
        context["session_emotion"] = self.working.emotion_summary()

        # Episodic memory — relevant past events
        context["episodic"] = self.episodic.recall(query, limit=4)

        # Semantic facts from legacy system
        if self._legacy_memory:
            try:
                legacy_ctx = self._legacy_memory.build_context(
                    query, short_term_limit=0, long_term_limit=4, semantic_limit=4
                )
                context["semantic_facts"] = [
                    item.get("text", item.get("content", ""))
                    for item in legacy_ctx.semantic if item
                ]
                context["preferences"] = legacy_ctx.long_term.get("preferences", {})
            except Exception as exc:
                logger.warning("Legacy memory retrieval failed: %s", exc)

        return context

    def end_session(self, session_id: str = "") -> list[dict[str, Any]]:
        """Consolidate working memory into long-term storage."""
        created = self.consolidator.consolidate(self.working, session_id=session_id)
        self.working.clear()
        # Run episodic forgetting
        forgotten = self.episodic.forget_stale()
        if forgotten:
            logger.info("Forgot %d stale episodic memories", forgotten)
        return created

    def remember_fact(self, fact: str, **meta: Any) -> bool:
        """Explicitly store a user fact in semantic memory."""
        if self._legacy_memory:
            return self._legacy_memory.remember("user_profile", fact, "fact", metadata=meta)
        return False

    def remember_preference(self, key: str, value: Any) -> None:
        if self._legacy_memory:
            self._legacy_memory.save_preference(key, value)
