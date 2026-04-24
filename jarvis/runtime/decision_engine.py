from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from jarvis.core.intent_classifier import IntentClassifier
from jarvis.runtime.input_handler import RuntimeInput
from jarvis.runtime.normalizer import InputNormalizer
from jarvis.core.safety.prescreen import SafetyPreScreen
from jarvis.runtime.intent_cache import IntentCache
from jarvis.runtime.rule_engine import RuleEngine, ActionPolicy
from jarvis.runtime.embedding_matcher import EmbeddingMatcher
from jarvis.core.memory.enricher import ContextEnricher

logger = logging.getLogger("Jarvis.DecisionEngine")


@dataclass(slots=True)
class RuntimeDecision:
    kind: str
    text: str = ""
    source: str = "text"
    interrupted: bool = False
    intent: str | None = None
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    tier_hint: dict[str, Any] | None = None
    permission_level: str = "SAFE"
    confidence: float = 1.0


class DecisionEngine:
    def __init__(
        self,
        *,
        normalizer: InputNormalizer,
        prescreen: SafetyPreScreen,
        cache: IntentCache,
        rule_engine: RuleEngine,
        embedding_matcher: EmbeddingMatcher,
        enricher: ContextEnricher,
        intent_classifier: IntentClassifier | None = None,
    ) -> None:
        self._normalizer = normalizer
        self._prescreen = prescreen
        self._cache = cache
        self._rule_engine = rule_engine
        self._embedding_matcher = embedding_matcher
        self._enricher = enricher
        self._intent_classifier = intent_classifier or IntentClassifier()

    def decide(self, command: RuntimeInput) -> RuntimeDecision:
        # Layer 0: PreScreen
        raw_text = command.text.strip()
        is_safe, reason = self._prescreen.check(raw_text)
        if not is_safe:
            return RuntimeDecision(
                kind="ignore",
                text=raw_text,
                source=command.source,
                interrupted=command.interrupted,
                args={"reason": reason}
            )

        # Layer 0: Normalize
        normalized_result = self._normalizer.normalize(raw_text)
        normalized_text = str(normalized_result)
        if not normalized_text:
            return RuntimeDecision(kind="ignore")

        # Layer 0: Intent Cache Check
        cached_policy = self._cache.get(normalized_text)
        if cached_policy is not None:
            return self._to_decision("execute_fast", cached_policy, command, normalized_text)

        # ── Tier 0: IntentClassifier (regex fast-path, <10ms) ─────────
        # Safety Gate: if normalization was unsafe (e.g. fuzzy match distortion), skip Tier 0
        if not getattr(normalized_result, "metadata", {}).get("unsafe_normalization"):
            fast = self._intent_classifier.classify(normalized_text)
            if fast.matched:
                policy = self._apply_policy(fast)
                
                # Confidence Floor: if Tier 0 is unsure, let the LLM take a look
                if policy.confidence < 0.9:
                    logger.info("Tier 0 confidence low (%.2f) — downgrading to Tier 1.", policy.confidence)
                else:
                    self._cache.set(normalized_text, policy)
                    logger.info(
                        "Tier-0 fast match: tool=%s args=%s (%.2f ms)",
                        fast.tool, fast.args, fast.elapsed_ms,
                    )
                    return self._to_decision("execute_fast", policy, command, normalized_text)
        else:
            logger.info("Skipping Tier 0 due to unsafe normalization (fuzzy matching used).")

        tier_hint: dict[str, Any] = {"tier0_attempted": True, "tier0_confidence": "no_match"}

        # Tier 1: RuleEngine (YAML Regex/Trie)
        rule_policy = self._rule_engine.process(normalized_text)
        if rule_policy is not None:
            self._cache.set(normalized_text, rule_policy)
            return self._to_decision("execute_fast", rule_policy, command, normalized_text)
        else:
            tier_hint["tier1_attempted"] = True
            tier_hint["tier1_confidence"] = "failed"

        # Tier 2: EmbeddingMatcher (Semantic similarity)
        embed_policy = self._embedding_matcher.process(normalized_text)
        if embed_policy is not None:
            self._cache.set(normalized_text, embed_policy)
            return self._to_decision("execute_fast", embed_policy, command, normalized_text)
        else:
            tier_hint["tier2_attempted"] = True
            tier_hint["tier2_confidence"] = "failed"

        # Tier 3: Escalate to LLM Brain
        logger.info("All fast tiers missed — escalating to LLM: %r", normalized_text)
        return RuntimeDecision(
            kind="execute",
            text=normalized_text,
            source=command.source,
            interrupted=command.interrupted,
            tier_hint=tier_hint
        )

    def cache_intent(self, text: str, intent: str, tool: str, args: dict[str, Any]) -> None:
        """Called by the orchestrator after a successful LLM plan resolution."""
        normalized_result = self._normalizer.normalize(text)
        
        # 1. Do not cache poisoned inputs (unsafe normalizations)
        if getattr(normalized_result, "metadata", {}).get("unsafe_normalization"):
            logger.info("Skipping intent cache for '%s' due to unsafe normalization.", text)
            return
            
        normalized = str(normalized_result)
        policy = ActionPolicy(
            intent=intent,
            tool=tool,
            args=args,
            confidence=1.0,
            permission_level="SAFE"
        )
        self._cache.set(normalized, policy)

    def _to_decision(self, kind: str, policy: ActionPolicy, command: RuntimeInput, text: str) -> RuntimeDecision:
        return RuntimeDecision(
            kind=kind,
            text=text,
            source=command.source,
            interrupted=command.interrupted,
            intent=policy.intent,
            tool=policy.tool,
            args=policy.args,
            permission_level=policy.permission_level,
            confidence=policy.confidence,
        )

    def _apply_policy(self, fast: FastResult) -> ActionPolicy:
        """Risk analysis and policy arbitration for fast-path intents."""
        perm = "safe"
        conf = fast.confidence
        
        # 1. Tool-level risk escalation
        if fast.tool in {"delete_folder", "remove_folder", "delete_file"}:
            perm = "dangerous"
        elif fast.tool in {"close_app", "system_action"}:
            perm = "moderate"
            
        # 2. Content-level risk escalation (e.g. external URLs)
        if fast.metadata.get("is_external"):
            # External URL navigation is promoted to MODERATE to prevent blind phishing
            perm = "moderate"
            
        # 3. Behavioral Overrides (Confirmation Fatigue Protection)
        # TODO: Check usage history here to auto-allow repeated safe actions
        
        return ActionPolicy(
            intent=fast.tool,
            tool=fast.tool,
            args=fast.args,
            confidence=conf,
            permission_level=perm,
        )
