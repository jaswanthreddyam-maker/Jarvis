from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from jarvis.core.context import ExecutionPlan
from jarvis.core.executor import ExecutionReport
from jarvis.runtime.embedding_matcher import IntentTemplateBank


class FeedbackLoop:
    """Automates the promotion of successful Tier 3 plans and demotion of failing Tier 1 rules."""

    def __init__(self, rules_path: Path | str, template_bank: IntentTemplateBank, required_successes: int = 3):
        self._rules_path = Path(rules_path)
        self._template_bank = template_bank
        self._required_successes = required_successes
        self._success_counts: dict[str, int] = {}
        self._lock = threading.RLock()

    def observe(self, text: str, plan: ExecutionPlan, report: ExecutionReport, tier_source: str = "tier3") -> None:
        """Called by the orchestrator after an execution finishes."""
        if report.success:
            self._handle_success(text, plan, tier_source)
        else:
            self._handle_failure(text, plan, tier_source)

    def _handle_success(self, text: str, plan: ExecutionPlan, tier_source: str) -> None:
        # We only promote plans that the LLM (Tier 3) successfully resolved
        if tier_source != "tier3":
            return
            
        if not plan.steps or len(plan.steps) != 1:
            return  # Multi-step plans are too complex for immediate Tier 1/2 promotion right now
            
        step = plan.steps[0]
        intent = plan.intent
        
        with self._lock:
            normalized = " ".join(text.strip().lower().split())
            self._success_counts[normalized] = self._success_counts.get(normalized, 0) + 1
            
            if self._success_counts[normalized] >= self._required_successes:
                # 1. Graduate to Tier 2 (Semantic Template Bank)
                self._template_bank.store_template(
                    text=text,
                    intent=intent,
                    tool=step.action,
                    args_map=step.params,
                    permission="SAFE"
                )
                
                # 2. Graduate to Tier 1 (YAML Rule)
                self._write_yaml_rule({
                    "id": intent,
                    "patterns": [f"^{normalized}$"],
                    "tool": step.action,
                    "args_map": step.params,
                    "confidence": 0.85, # Start relatively high but demotable
                    "permission": "SAFE",
                    "requires_confirm": False
                })
                
                # Reset count to avoid spamming
                self._success_counts[normalized] = 0

    def _handle_failure(self, text: str, plan: ExecutionPlan, tier_source: str) -> None:
        # We demote Tier 1 rules that confidently matched but failed execution
        if tier_source == "tier1":
            self._demote_yaml_rule(plan.intent)

    def _read_yaml(self) -> dict[str, Any]:
        if not self._rules_path.exists():
            return {"rules": []}
        try:
            with open(self._rules_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {"rules": []}
        except Exception:
            return {"rules": []}

    def _write_yaml(self, data: dict[str, Any]) -> None:
        try:
            with open(self._rules_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, indent=2, sort_keys=False)
        except Exception:
            pass

    def _write_yaml_rule(self, new_rule: dict[str, Any]) -> None:
        data = self._read_yaml()
        if "rules" not in data:
            data["rules"] = []
            
        # Deduplicate patterns
        for r in data["rules"]:
            if r.get("id") == new_rule["id"] and new_rule["patterns"][0] in r.get("patterns", []):
                return
                
        data["rules"].append(new_rule)
        self._write_yaml(data)

    def _demote_yaml_rule(self, intent_id: str) -> None:
        with self._lock:
            data = self._read_yaml()
            dirty = False
            for r in data.get("rules", []):
                if r.get("id") == intent_id:
                    current_conf = float(r.get("confidence", 0.0))
                    new_conf = max(0.0, current_conf - 0.1)
                    r["confidence"] = round(new_conf, 2)
                    
                    # Demote completely if it falls below threshold
                    if new_conf < 0.5:
                        r["disabled"] = True
                    dirty = True
            
            if dirty:
                self._write_yaml(data)
