"""Reasoning Engine — transverses the Knowledge Graph to connect concepts and explain them.

Capabilities:
    - Multi-path graph traversal
    - Edge weighting and confidence scoring
    - Natural language explanation generation
"""
from __future__ import annotations

import logging
from typing import TypedDict
from collections import deque
from assistant.knowledge_graph import knowledge_graph, ConceptNode, ConceptEdge

logger = logging.getLogger("Jarvis.ReasoningEngine")

# Confidence weights for relation types
EDGE_WEIGHTS = {
    "is_a": 1.0,
    "part_of": 0.9,
    "depends_on": 0.85,
    "used_for": 0.8,
    "related_to": 0.7,
}

CONFIDENCE_THRESHOLD = 0.5

class ReasoningResult(TypedDict):
    explanation: str
    confidence: float
    path: list[ConceptEdge]

class ReasoningEngine:

    def __init__(self) -> None:
        self.graph = knowledge_graph

    def reason(self, concepts: list[str]) -> ReasoningResult | None:
        """Find the strongest reasoning path between extracted concepts."""
        if not concepts:
            return None
            
        nodes = []
        for c in concepts:
            matches = self.graph.search_nodes(c)
            if matches:
                nodes.append(matches[0])
                
        if len(nodes) < 2:
            if len(nodes) == 1:
                exp = self._explain_single(nodes[0])
                if exp:
                    return {"explanation": exp, "confidence": 0.9, "path": []}
            return None
            
        # Multi-path search between the first two matched concepts
        start_id = nodes[0].id
        end_id = nodes[1].id
        
        paths = self._find_all_paths(start_id, end_id, max_depth=4)
        if not paths:
            return None
            
        # Rank paths by confidence score
        scored_paths = []
        for p in paths:
            score = self._calculate_path_confidence(p)
            scored_paths.append((score, p))
            
        scored_paths.sort(key=lambda x: x[0], reverse=True)
        best_score, best_path = scored_paths[0]
        
        if best_score < CONFIDENCE_THRESHOLD:
            logger.info("Best reasoning path confidence %.2f < threshold %.2f", best_score, CONFIDENCE_THRESHOLD)
            return None
            
        explanation = self._generate_explanation(best_path, start_id, end_id)
        
        return {
            "explanation": explanation,
            "confidence": best_score,
            "path": best_path
        }

    def _find_all_paths(self, start_id: str, end_id: str, max_depth: int) -> list[list[ConceptEdge]]:
        """DFS to find all paths up to max_depth between two nodes."""
        all_paths = []
        
        def dfs(current_id: str, current_path: list[ConceptEdge], visited: set[str]):
            if current_id == end_id:
                all_paths.append(list(current_path))
                return
                
            if len(current_path) >= max_depth:
                return
                
            for edge in self.graph.get_neighbors(current_id):
                next_id = edge.target if edge.source == current_id else edge.source
                if next_id not in visited:
                    visited.add(next_id)
                    current_path.append(edge)
                    dfs(next_id, current_path, visited)
                    current_path.pop()
                    visited.remove(next_id)
                    
        dfs(start_id, [], set([start_id]))
        return all_paths

    def _calculate_path_confidence(self, path: list[ConceptEdge]) -> float:
        """Calculate confidence based on edge weights and length."""
        if not path:
            return 0.0
            
        confidence = 1.0
        for edge in path:
            base_weight = EDGE_WEIGHTS.get(edge.type, 0.6)
            # Factor in edge frequency/strength from the graph
            dynamic_weight = min(1.2, edge.weight) 
            confidence *= (base_weight * dynamic_weight)
            
        # Penalize longer paths slightly
        confidence *= (0.95 ** (len(path) - 1))
        return min(1.0, confidence)

    def _explain_single(self, node: ConceptNode) -> str | None:
        edges = self.graph.get_neighbors(node.id)
        if not edges:
            return None
            
        # Score edges to pick the most defining ones
        scored_edges = []
        for e in edges:
            w = EDGE_WEIGHTS.get(e.type, 0.6) * e.weight
            scored_edges.append((w, e))
        scored_edges.sort(key=lambda x: x[0], reverse=True)
            
        parts = [f"{node.label} is a {node.type}."]
        for _, e in scored_edges[:2]:
            other_id = e.target if e.source == node.id else e.source
            other_node = self.graph.get_node(other_id)
            if not other_node: continue
            
            rel = e.type.replace("_", " ")
            if e.source == node.id:
                parts.append(f"It {rel} {other_node.label}.")
            else:
                parts.append(f"{other_node.label} {rel} it.")
                
        return " ".join(parts)

    def _generate_explanation(self, path: list[ConceptEdge], start_id: str, end_id: str) -> str:
        """Convert a path into a natural, human-readable sentence."""
        if not path:
            return ""
            
        sentences = []
        current_id = start_id
        
        for i, edge in enumerate(path):
            next_id = edge.target if edge.source == current_id else edge.source
            source_node = self.graph.get_node(current_id)
            target_node = self.graph.get_node(next_id)
            
            if not source_node or not target_node:
                current_id = next_id
                continue
                
            rel = edge.type.replace("_", " ")
            
            if edge.source == current_id:
                clause = f"{source_node.label} {rel} {target_node.label}"
            else:
                # Reverse relation naturally
                if edge.type == "part_of":
                    clause = f"{target_node.label} contains {source_node.label}"
                elif edge.type == "used_for":
                    clause = f"{target_node.label} relies on {source_node.label}"
                elif edge.type == "depends_on":
                    clause = f"{target_node.label} is required by {source_node.label}"
                elif edge.type == "is_a":
                    clause = f"{target_node.label} is a type of {source_node.label}"
                else:
                    clause = f"{target_node.label} is {rel} {source_node.label}"
            
            sentences.append(clause)
            current_id = next_id
            
        if len(sentences) == 1:
            return f"They are directly connected: {sentences[0].capitalize()}."
        elif len(sentences) == 2:
            return f"Here is the connection: {sentences[0]}, and {sentences[1]}."
        else:
            joined = ", ".join(sentences[:-1]) + f", which means {sentences[-1]}"
            return f"Logically: {joined.capitalize()}."

# Singleton
reasoner = ReasoningEngine()
