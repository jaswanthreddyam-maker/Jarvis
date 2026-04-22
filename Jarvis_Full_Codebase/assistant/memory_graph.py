"""Memory Graph — relational brain for Jarvis.

Stores contextual relationships between user, apps, habits, time patterns,
and preferences as a persistent, queryable graph.

Features:
    - Nodes: user, app, action, time_pattern, preference
    - Edges: uses, prefers, frequent_time, related_to, triggers, results_in
    - Persistence: JSON-backed, auto-saved
    - Decay: outdated edges lose weight over time
    - Composite scoring: frequency × recency (stale habits naturally fade)
    - Feedback: boost / penalize edges from user accept/reject
    - Pattern query: "what does user do at 8 PM?"
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("Jarvis.MemoryGraph")

DECAY_HALF_LIFE_DAYS = 14.0
BOOST_DECAY_HALF_LIFE_DAYS = 7.0   # Feedback penalties/boosts fade in ~1 week
MIN_EDGE_WEIGHT = 0.05
STORAGE_FILENAME = "memory_graph.json"
# Diminishing returns: each new reinforcement adds less than the previous one
_REINFORCE_DIMINISH = 0.85  # multiplier per existing count


class GraphNode:
    __slots__ = ("id", "type", "data", "created_at", "last_accessed")

    def __init__(self, id: str, node_type: str, data: dict | None = None):
        self.id = id
        self.type = node_type
        self.data = data or {}
        self.created_at = time.time()
        self.last_accessed = time.time()

    def touch(self):
        self.last_accessed = time.time()

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "data": self.data,
                "created_at": self.created_at, "last_accessed": self.last_accessed}

    @classmethod
    def from_dict(cls, d: dict) -> GraphNode:
        n = cls(d["id"], d["type"], d.get("data", {}))
        n.created_at = d.get("created_at", time.time())
        n.last_accessed = d.get("last_accessed", time.time())
        return n


class GraphEdge:
    __slots__ = ("source_id", "target_id", "relationship", "weight",
                 "created_at", "last_updated", "count", "boost")

    def __init__(self, source_id: str, target_id: str, relationship: str, weight: float = 1.0):
        self.source_id = source_id
        self.target_id = target_id
        self.relationship = relationship
        self.weight = weight
        self.created_at = time.time()
        self.last_updated = time.time()
        self.count = 1
        self.boost = 0.0  # cumulative boost from user feedback

    def reinforce(self, amount: float = 1.0):
        """Add weight with diminishing returns so high-count edges
        don't grow unboundedly from repetition alone."""
        effective = amount * (_REINFORCE_DIMINISH ** self.count)
        self.weight += max(effective, 0.1)  # floor so it always grows a bit
        self.count += 1
        self.last_updated = time.time()

    def decayed_weight(self) -> float:
        """Pure time-decay based weight."""
        age_days = (time.time() - self.last_updated) / 86400.0
        return self.weight * math.exp(-0.693 * age_days / DECAY_HALF_LIFE_DAYS)

    def composite_score(self) -> float:
        """Blended score: frequency * recency * feedback.

        - frequency: log(count + 1) prevents one dimension from dominating
        - recency: exponential decay on age since last use
        - boost: additive modifier that itself decays toward 0 over time
          so old rejections don't permanently suppress a pattern
        """
        age_days = (time.time() - self.last_updated) / 86400.0
        recency = math.exp(-0.693 * age_days / DECAY_HALF_LIFE_DAYS)
        frequency = math.log(self.count + 1)
        # Decay boost toward zero with its own half-life
        decayed_boost = self.boost * math.exp(
            -0.693 * age_days / BOOST_DECAY_HALF_LIFE_DAYS
        )
        raw = self.weight * recency * frequency + decayed_boost
        return max(0.0, raw)

    def apply_boost(self, delta: float) -> None:
        """Apply feedback boost (positive = accepted, negative = rejected)."""
        self.boost += delta
        self.last_updated = time.time()

    def to_dict(self) -> dict:
        return {"source_id": self.source_id, "target_id": self.target_id,
                "relationship": self.relationship, "weight": self.weight,
                "created_at": self.created_at, "last_updated": self.last_updated,
                "count": self.count, "boost": self.boost}

    @classmethod
    def from_dict(cls, d: dict) -> GraphEdge:
        e = cls(d["source_id"], d["target_id"], d["relationship"], d.get("weight", 1.0))
        e.created_at = d.get("created_at", time.time())
        e.last_updated = d.get("last_updated", time.time())
        e.count = d.get("count", 1)
        e.boost = d.get("boost", 0.0)
        return e


class MemoryGraph:
    def __init__(self, storage_dir: Path | str | None = None):
        if storage_dir is None:
            storage_dir = Path(__file__).resolve().parents[1] / "memory"
        self._storage_path = Path(storage_dir) / STORAGE_FILENAME
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []
        self._edge_index: dict[str, list[int]] = defaultdict(list)
        self._lock = threading.Lock()
        self._load()
        self.add_node("user:primary", "user", {"label": "primary_user"})
        self._running = True
        self._save_thread = threading.Thread(
            target=self._periodic_save, daemon=True, name="MemoryGraph-Saver")
        self._save_thread.start()

    # ── Node ops ─────────────────────────────────────────────────────
    def add_node(self, node_id: str, node_type: str, data: dict | None = None) -> GraphNode:
        with self._lock:
            if node_id in self._nodes:
                self._nodes[node_id].touch()
                if data:
                    self._nodes[node_id].data.update(data)
                return self._nodes[node_id]
            node = GraphNode(node_id, node_type, data)
            self._nodes[node_id] = node
            return node

    def get_node(self, node_id: str) -> GraphNode | None:
        with self._lock:
            return self._nodes.get(node_id)

    def get_nodes_by_type(self, node_type: str) -> list[GraphNode]:
        with self._lock:
            return [n for n in self._nodes.values() if n.type == node_type]

    # ── Edge ops ─────────────────────────────────────────────────────
    def add_edge(self, source_id: str, target_id: str, relationship: str, weight: float = 1.0):
        with self._lock:
            if source_id not in self._nodes or target_id not in self._nodes:
                return
            for idx in self._edge_index.get(source_id, []):
                edge = self._edges[idx]
                if edge.target_id == target_id and edge.relationship == relationship:
                    edge.reinforce(weight)
                    return
            edge = GraphEdge(source_id, target_id, relationship, weight)
            idx = len(self._edges)
            self._edges.append(edge)
            self._edge_index[source_id].append(idx)

    def query_related(self, source_id: str, relationship: str | None = None,
                      min_weight: float = 0.1, use_composite: bool = False
                      ) -> list[tuple[str, float, str]]:
        """Query edges from source_id.

        Args:
            use_composite: If True, score by composite_score() instead of
                           decayed_weight().  Composite blends frequency +
                           recency + feedback for more realistic ranking.
        """
        with self._lock:
            results = []
            for idx in self._edge_index.get(source_id, []):
                edge = self._edges[idx]
                if relationship and edge.relationship != relationship:
                    continue
                score = edge.composite_score() if use_composite else edge.decayed_weight()
                if score >= min_weight:
                    results.append((edge.target_id, score, edge.relationship))
            return sorted(results, key=lambda x: x[1], reverse=True)

    def query_reverse(self, target_id: str, relationship: str | None = None) -> list[tuple[str, float, str]]:
        with self._lock:
            results = []
            for edge in self._edges:
                if edge.target_id != target_id:
                    continue
                if relationship and edge.relationship != relationship:
                    continue
                dw = edge.decayed_weight()
                if dw >= MIN_EDGE_WEIGHT:
                    results.append((edge.source_id, dw, edge.relationship))
            return sorted(results, key=lambda x: x[1], reverse=True)

    # ── High-level recording ─────────────────────────────────────────
    def record_interaction(self, user_input: str, intent_type: str,
                           result_state: str, app: str | None = None):
        now = datetime.now()
        hour = now.hour
        day_name = now.strftime("%A").lower()
        input_norm = user_input.strip().lower()[:80]
        input_id = f"action:{input_norm}"
        intent_id = f"intent:{intent_type}"
        time_id = f"time:{hour:02d}"
        day_id = f"day:{day_name}"

        self.add_node(input_id, "action", {"raw": user_input})
        self.add_node(intent_id, "intent")
        self.add_node(time_id, "time_pattern", {"hour": hour})
        self.add_node(day_id, "time_pattern", {"day": day_name})

        self.add_edge("user:primary", input_id, "performs")
        self.add_edge(input_id, intent_id, "triggers")
        self.add_edge(input_id, time_id, "occurs_at")
        self.add_edge(input_id, day_id, "occurs_on")
        self.add_edge("user:primary", time_id, "active_at")

        if app:
            app_id = f"app:{app.lower()}"
            self.add_node(app_id, "app", {"name": app})
            self.add_edge("user:primary", app_id, "uses")
            self.add_edge(input_id, app_id, "targets")
            self.add_edge(app_id, time_id, "used_at")

    def record_preference(self, key: str, value: str):
        pref_id = f"pref:{key.lower()}"
        self.add_node(pref_id, "preference", {"key": key, "value": value})
        self.add_edge("user:primary", pref_id, "prefers")

    def record_app_usage(self, app_name: str):
        app_id = f"app:{app_name.lower()}"
        time_id = f"time:{datetime.now().hour:02d}"
        self.add_node(app_id, "app", {"name": app_name})
        self.add_node(time_id, "time_pattern", {"hour": datetime.now().hour})
        self.add_edge("user:primary", app_id, "uses")
        self.add_edge(app_id, time_id, "used_at")

    # ── Pattern queries ──────────────────────────────────────────────
    def get_habits_at_hour(self, hour: int) -> list[tuple[str, float]]:
        """Return actions / apps associated with this hour, scored by
        composite (frequency × recency × feedback) so stale habits
        naturally drop off."""
        time_id = f"time:{hour:02d}"
        incoming = self._query_reverse_composite(time_id)
        results = [(sid, w) for sid, w, _ in incoming if sid.startswith(("action:", "app:"))]
        return sorted(results, key=lambda x: x[1], reverse=True)

    def _query_reverse_composite(self, target_id: str,
                                  relationship: str | None = None
                                  ) -> list[tuple[str, float, str]]:
        """Like query_reverse but uses composite_score."""
        with self._lock:
            results = []
            for edge in self._edges:
                if edge.target_id != target_id:
                    continue
                if relationship and edge.relationship != relationship:
                    continue
                score = edge.composite_score()
                if score >= MIN_EDGE_WEIGHT:
                    results.append((edge.source_id, score, edge.relationship))
            return sorted(results, key=lambda x: x[1], reverse=True)

    def get_frequent_apps(self, min_weight: float = 2.0) -> list[tuple[str, float]]:
        related = self.query_related("user:primary", relationship="uses",
                                     min_weight=min_weight, use_composite=True)
        return [(tid, w) for tid, w, _ in related if tid.startswith("app:")]

    def get_user_preferences(self) -> dict[str, str]:
        prefs = {}
        for tid, _, _ in self.query_related("user:primary", relationship="prefers"):
            node = self.get_node(tid)
            if node and node.data:
                prefs[node.data.get("key", tid)] = node.data.get("value", "")
        return prefs

    # ── Feedback mutations ───────────────────────────────────────────
    def boost_edge(self, source_id: str, target_id: str,
                   relationship: str | None = None, delta: float = 2.0) -> bool:
        """Boost an edge's score (user accepted a suggestion)."""
        with self._lock:
            for idx in self._edge_index.get(source_id, []):
                edge = self._edges[idx]
                if edge.target_id == target_id:
                    if relationship and edge.relationship != relationship:
                        continue
                    edge.apply_boost(delta)
                    logger.debug("Boosted edge %s→%s by +%.1f", source_id, target_id, delta)
                    return True
        return False

    def penalize_edge(self, source_id: str, target_id: str,
                      relationship: str | None = None, delta: float = 3.0) -> bool:
        """Penalize an edge's score (user rejected a suggestion)."""
        with self._lock:
            for idx in self._edge_index.get(source_id, []):
                edge = self._edges[idx]
                if edge.target_id == target_id:
                    if relationship and edge.relationship != relationship:
                        continue
                    edge.apply_boost(-delta)  # negative boost = penalty
                    logger.debug("Penalized edge %s→%s by -%.1f", source_id, target_id, delta)
                    return True
        return False

    def get_edge_raw(self, source_id: str, target_id: str,
                     relationship: str | None = None) -> dict | None:
        """Get raw edge data for debugging."""
        with self._lock:
            for idx in self._edge_index.get(source_id, []):
                edge = self._edges[idx]
                if edge.target_id == target_id:
                    if relationship and edge.relationship != relationship:
                        continue
                    return edge.to_dict()
        return None

    # ── Decay + Pruning ──────────────────────────────────────────────
    def prune_decayed(self) -> int:
        with self._lock:
            original = len(self._edges)
            surviving = []
            new_index: dict[str, list[int]] = defaultdict(list)
            for edge in self._edges:
                if edge.decayed_weight() >= MIN_EDGE_WEIGHT:
                    idx = len(surviving)
                    surviving.append(edge)
                    new_index[edge.source_id].append(idx)
            self._edges = surviving
            self._edge_index = new_index
        pruned = original - len(surviving)
        if pruned:
            logger.info("Pruned %d decayed edges", pruned)
        return pruned

    def clear_all(self):
        with self._lock:
            self._nodes.clear()
            self._edges.clear()
            self._edge_index.clear()
        self._save()
        self.add_node("user:primary", "user", {"label": "primary_user"})
        logger.warning("Memory graph cleared by user request.")

    def stats(self) -> dict[str, Any]:
        with self._lock:
            tc: dict[str, int] = defaultdict(int)
            for n in self._nodes.values():
                tc[n.type] += 1
            return {"total_nodes": len(self._nodes), "total_edges": len(self._edges),
                    "node_types": dict(tc)}

    # ── Persistence ──────────────────────────────────────────────────
    def _save(self):
        try:
            with self._lock:
                data = {"nodes": [n.to_dict() for n in self._nodes.values()],
                        "edges": [e.to_dict() for e in self._edges]}
            with open(self._storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save memory graph: %s", e)

    def _load(self):
        if not self._storage_path.exists():
            return
        try:
            with open(self._storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for nd in data.get("nodes", []):
                node = GraphNode.from_dict(nd)
                self._nodes[node.id] = node
            for ed in data.get("edges", []):
                edge = GraphEdge.from_dict(ed)
                idx = len(self._edges)
                self._edges.append(edge)
                self._edge_index[edge.source_id].append(idx)
            logger.info("Loaded memory graph: %d nodes, %d edges",
                        len(self._nodes), len(self._edges))
        except Exception as e:
            logger.error("Failed to load memory graph: %s", e)

    def decay_boosts(self) -> int:
        """Actively write decayed boost values back to edges so the
        JSON snapshot reflects current reality.  Returns count touched."""
        touched = 0
        now = time.time()
        with self._lock:
            for edge in self._edges:
                if abs(edge.boost) < 0.01:
                    continue
                age_days = (now - edge.last_updated) / 86400.0
                factor = math.exp(-0.693 * age_days / BOOST_DECAY_HALF_LIFE_DAYS)
                new_boost = edge.boost * factor
                if abs(new_boost) < 0.05:
                    new_boost = 0.0
                if new_boost != edge.boost:
                    edge.boost = new_boost
                    touched += 1
        if touched:
            logger.debug("Decayed boosts on %d edges", touched)
        return touched

    def _periodic_save(self):
        counter = 0
        while self._running:
            time.sleep(30.0)
            self._save()
            counter += 1
            if counter >= 20:
                self.prune_decayed()
                self.decay_boosts()
                counter = 0

    def stop(self):
        self._running = False
        self._save()


graph = MemoryGraph()
