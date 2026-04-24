from __future__ import annotations

from typing import Any, Callable


class ContextEnricher:
    """Standardizes how each execution tier consumes memory and system context."""

    def __init__(
        self,
        *,
        short_term_memory: Any,
        long_term_memory: Any,
        semantic_memory: Any,
        system_state_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.short_term = short_term_memory
        self.long_term = long_term_memory
        self.semantic = semantic_memory
        self._system_state_provider = system_state_provider

    def enrich_for_tier1(self, text: str) -> dict[str, Any]:
        """Ultra-low latency context for regex rules."""
        aliases = self.long_term.get_preference("aliases", {}) if hasattr(self.long_term, "get_preference") else {}
        
        return {
            "last_action": self.short_term.last_interaction() if hasattr(self.short_term, "last_interaction") else None,
            "aliases": aliases,
        }

    def enrich_for_tier2(self, text: str) -> dict[str, Any]:
        """Context for embedding matchers."""
        templates = self.semantic.retrieve_similar(text, limit=5) if hasattr(self.semantic, "retrieve_similar") else []
        
        return {
            "templates": templates,
            "last_action": self.short_term.last_interaction() if hasattr(self.short_term, "last_interaction") else None,
        }

    def enrich_for_tier3(self, text: str) -> dict[str, Any]:
        """Full context for LLM pipeline including system state."""
        recent = self.short_term.recent(limit=6) if hasattr(self.short_term, "recent") else []
        preferences = self.long_term.preferences() if hasattr(self.long_term, "preferences") else {}
        semantic_matches = self.semantic.retrieve_similar(text, limit=3) if hasattr(self.semantic, "retrieve_similar") else []
        
        # Pull system state directly at enrichment time
        system_state = self._gather_system_state()

        return {
            "recent": recent,
            "preferences": preferences,
            "semantic": semantic_matches,
            "last_action": self.short_term.last_interaction() if hasattr(self.short_term, "last_interaction") else None,
            "system_state": system_state,
        }

    def _gather_system_state(self) -> dict[str, Any]:
        """Gathers active window, clipboard, and running apps."""
        if self._system_state_provider:
            try:
                return self._system_state_provider()
            except Exception:
                return {}
                
        state: dict[str, Any] = {}
        
        # Fallback inline clipboard retrieval if no provider is set
        try:
            from jarvis.infrastructure.system_control.system_tools import get_clipboard
            clip_res = get_clipboard()
            if clip_res and clip_res.success:
                state["clipboard"] = clip_res.data.get("text", "")
        except Exception:
            pass
            
        # Placeholders for active_window and running_apps if provider is missing
        # A full system state provider should be injected during DecisionEngine wire-up
        if "active_window" not in state:
            state["active_window"] = "unknown"
        if "running_apps" not in state:
            state["running_apps"] = []
            
        return state
