"""Knowledge Graph — stores concepts, technologies, and domains to enable reasoning.

Nodes:
    - id: string (e.g., "concept:neural_networks")
    - type: "concept", "technology", "tool", "domain"
    - label: string (e.g., "Neural Networks")

Edges:
    - source: node_id
    - target: node_id
    - type: "is_a", "related_to", "used_for", "depends_on", "part_of"
    - weight: float
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("Jarvis.KnowledgeGraph")


@dataclass
class ConceptNode:
    id: str
    type: str
    label: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "metadata": self.metadata,
        }


@dataclass
class ConceptEdge:
    source: str
    target: str
    type: str
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "type": self.type,
            "weight": self.weight,
        }


class KnowledgeGraph:
    """In-memory concept graph with JSON persistence."""

    def __init__(self, storage_dir: Path | str | None = None) -> None:
        if storage_dir is None:
            storage_dir = Path(__file__).resolve().parents[1] / "memory"
        self._storage_path = Path(storage_dir) / "knowledge_graph.json"
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)

        self._nodes: dict[str, ConceptNode] = {}
        self._edges: list[ConceptEdge] = []
        self._adjacency: dict[str, list[ConceptEdge]] = {}
        self._lock = threading.RLock()
        
        self._load()

    def _load(self) -> None:
        if not self._storage_path.exists():
            # Initialize with basic concepts if empty
            self._bootstrap()
            return
            
        try:
            with open(self._storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            for n in data.get("nodes", []):
                node = ConceptNode(**n)
                self._nodes[node.id] = node
                
            for e in data.get("edges", []):
                edge = ConceptEdge(**e)
                self._edges.append(edge)
                self._adjacency.setdefault(edge.source, []).append(edge)
                self._adjacency.setdefault(edge.target, []).append(edge) # Bidirectional traversal capability
                
            logger.info("Loaded Knowledge Graph: %d nodes, %d edges", len(self._nodes), len(self._edges))
        except Exception as e:
            logger.error("Failed to load knowledge graph: %s", e)
            self._bootstrap()

    def _save(self) -> None:
        try:
            data = {
                "nodes": [n.to_dict() for n in self._nodes.values()],
                "edges": [e.to_dict() for e in self._edges],
            }
            with open(self._storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save knowledge graph: %s", e)

    def _bootstrap(self) -> None:
        """Seed the graph with initial concepts for AI."""
        with self._lock:
            self.add_node("concept:ai", "domain", "Artificial Intelligence", save=False)
            self.add_node("concept:ml", "domain", "Machine Learning", save=False)
            self.add_node("technology:python", "technology", "Python", save=False)
            self.add_node("concept:neural_networks", "concept", "Neural Networks", save=False)
            
            self.add_edge("concept:ml", "concept:ai", "part_of", save=False)
            self.add_edge("technology:python", "concept:ai", "used_for", save=False)
            self.add_edge("concept:neural_networks", "concept:ml", "part_of", save=False)
            self._save()

    def add_node(self, id: str, type: str, label: str, metadata: dict | None = None, save: bool = True) -> None:
        with self._lock:
            if id not in self._nodes:
                self._nodes[id] = ConceptNode(id, type, label, metadata or {})
                if save:
                    self._save()

    def add_edge(self, source: str, target: str, type: str, weight: float = 1.0, save: bool = True) -> None:
        with self._lock:
            # Check if edge exists
            for e in self._edges:
                if e.source == source and e.target == target and e.type == type:
                    e.weight += 0.1  # Reinforce
                    if save:
                        self._save()
                    return
                    
            edge = ConceptEdge(source, target, type, weight)
            self._edges.append(edge)
            self._adjacency.setdefault(source, []).append(edge)
            self._adjacency.setdefault(target, []).append(edge)
            if save:
                self._save()

    def get_node(self, id: str) -> ConceptNode | None:
        with self._lock:
            return self._nodes.get(id)

    def get_neighbors(self, node_id: str) -> list[ConceptEdge]:
        with self._lock:
            return list(self._adjacency.get(node_id, []))

    def search_nodes(self, query: str) -> list[ConceptNode]:
        """Find nodes matching a keyword."""
        q = query.lower()
        with self._lock:
            return [n for n in self._nodes.values() if q in n.label.lower() or q in n.id.lower()]

# Singleton
knowledge_graph = KnowledgeGraph()
