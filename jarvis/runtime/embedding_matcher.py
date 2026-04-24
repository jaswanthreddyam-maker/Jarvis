from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from jarvis.core.memory.semantic import SemanticMemory
from jarvis.core.memory.enricher import ContextEnricher
from jarvis.runtime.rule_engine import ActionPolicy


class IntentTemplateBank:
    """Stores and retrieves Intent Templates from Semantic Memory (ChromaDB)."""
    
    def __init__(self, semantic_memory: SemanticMemory):
        self._memory = semantic_memory

    def store_template(
        self, 
        text: str, 
        intent: str, 
        tool: str, 
        args_map: dict[str, Any], 
        permission: str = "SAFE", 
        requires_confirm: bool = False
    ) -> None:
        self._memory.store_memory(
            text=text,
            metadata={
                "type": "intent_template",
                "intent": intent,
                "tool": tool,
                "args_map": str(args_map), # ChromaDB requires primitives
                "permission": permission,
                "requires_confirm": requires_confirm
            }
        )

    def retrieve_best_match(self, text: str, threshold: float = 0.85) -> dict[str, Any] | None:
        results = self._memory.retrieve_similar(text, limit=3, min_score=threshold)
        
        for best in results:
            meta = best.get("metadata", {})
            if meta.get("type") == "intent_template":
                return {
                    "text": best["text"],
                    "score": best["score"],
                    "intent": meta.get("intent", "unknown"),
                    "tool": meta.get("tool", "unknown"),
                    "args_map": meta.get("args_map", "{}"),
                    "permission": meta.get("permission", "SAFE"),
                    "requires_confirm": str(meta.get("requires_confirm", "False")).lower() == "true",
                }
        return None


class SlotFiller:
    def __init__(self, enricher: ContextEnricher):
        self._enricher = enricher

    def fill(self, template: str, query: str, args_map_str: str) -> dict[str, Any]:
        """
        Aligns the query with the template to extract values.
        Resolves pronouns like 'it' using the Tier 2 last_action context.
        """
        try:
            args_map = ast.literal_eval(args_map_str)
        except Exception:
            args_map = {}
            
        context = self._enricher.enrich_for_tier2(query)
        last_action = context.get("last_action") or {}
        
        # Depending on how the dict is structured from interaction.as_dict()
        last_metadata = last_action.get("metadata", {})
        last_args = last_metadata.get("args", {}) if "args" in last_metadata else last_action.get("params", {})
        
        filled_args = {}
        query_words = query.lower().split()
        
        for key, val_template in args_map.items():
            if isinstance(val_template, str):
                normalized_val = val_template.lower()
                # Pronoun resolution utilizing ContextEnricher's last_action
                if normalized_val == "it" and last_args:
                    # simplistic fallback: just grab the first value from the last action args
                    filled_args[key] = next(iter(last_args.values()), "it")
                elif val_template.startswith("<alias:") and val_template.endswith(">"):
                    # Basic extraction fallback (assuming the last word is the target)
                    # In a production system, this would use a more robust token-alignment or NER model
                    filled_args[key] = query_words[-1] if query_words else ""
                else:
                    filled_args[key] = val_template
            else:
                filled_args[key] = val_template
                
        return filled_args


class EmbeddingMatcher:
    def __init__(
        self, 
        template_bank: IntentTemplateBank, 
        enricher: ContextEnricher, 
        threshold: float = 0.85
    ) -> None:
        self._bank = template_bank
        self._slot_filler = SlotFiller(enricher)
        self.threshold = threshold

    def process(self, text: str) -> ActionPolicy | None:
        """Calculates cosine similarity. If >= threshold, returns extracted ActionPolicy."""
        match = self._bank.retrieve_best_match(text, threshold=self.threshold)
        if not match:
            return None
            
        args = self._slot_filler.fill(match["text"], text, match["args_map"])
        
        return ActionPolicy(
            intent=match["intent"],
            tool=match["tool"],
            args=args,
            confidence=match["score"],
            permission_level=match["permission"],
            requires_confirm=match["requires_confirm"]
        )
