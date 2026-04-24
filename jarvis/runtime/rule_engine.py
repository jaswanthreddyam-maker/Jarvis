from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jarvis.core.memory.enricher import ContextEnricher


@dataclass(slots=True)
class ActionPolicy:
    intent: str
    tool: str
    args: dict[str, Any]
    confidence: float
    permission_level: str
    requires_confirm: bool = False


@dataclass(slots=True)
class RuleDefinition:
    id: str
    patterns: list[str]
    tool: str
    args_map: dict[str, Any]
    confidence: float
    permission: str
    requires_confirm: bool = False
    regexes: list[re.Pattern] = field(init=False, repr=False)

    def __post_init__(self):
        # Pre-compile regexes for ultra-low latency matching
        self.regexes = [re.compile(p, re.IGNORECASE) for p in self.patterns]


class RuleLoader:
    def __init__(self, rule_file_path: Path | str) -> None:
        self._path = Path(rule_file_path)
        self._last_mtime = 0.0
        self._rules: list[RuleDefinition] = []
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    def start_watchdog(self, interval_sec: float = 2.0) -> None:
        if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
            self._stop_event.clear()
            self._watchdog_thread = threading.Thread(target=self._watch_loop, args=(interval_sec,), daemon=True)
            self._watchdog_thread.start()

    def stop_watchdog(self) -> None:
        self._stop_event.set()
        if self._watchdog_thread:
            self._watchdog_thread.join()

    def _watch_loop(self, interval_sec: float) -> None:
        while not self._stop_event.is_set():
            self.reload_if_changed()
            self._stop_event.wait(interval_sec)

    def reload_if_changed(self) -> None:
        if not self._path.exists():
            return
        mtime = self._path.stat().st_mtime
        if mtime > self._last_mtime:
            self.force_reload()
            self._last_mtime = mtime

    def force_reload(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            
            new_rules = []
            for rule_data in data.get("rules", []):
                if rule_data.get("disabled", False):
                    continue
                rule = RuleDefinition(
                    id=rule_data.get("id", "unknown"),
                    patterns=rule_data.get("patterns", []),
                    tool=rule_data.get("tool", "unknown"),
                    args_map=rule_data.get("args_map", {}),
                    confidence=float(rule_data.get("confidence", 0.0)),
                    permission=rule_data.get("permission", "SAFE"),
                    requires_confirm=bool(rule_data.get("requires_confirm", False)),
                )
                new_rules.append(rule)
            
            with self._lock:
                self._rules = new_rules
        except Exception:
            # Silently ignore parsing errors during hot-reload to keep old stable rules
            pass

    def get_rules(self) -> list[RuleDefinition]:
        with self._lock:
            return list(self._rules)


class AliasResolver:
    def __init__(self, enricher: ContextEnricher) -> None:
        self._enricher = enricher

    def resolve(self, args_map: dict[str, Any], match_dict: dict[str, str], text: str) -> dict[str, Any]:
        """Resolves alias tags like <alias:KNOWN_URLS[site]> from args_map using the enricher context."""
        context = self._enricher.enrich_for_tier1(text)
        aliases = context.get("aliases", {})
        
        resolved_args = {}
        for key, val_template in args_map.items():
            if isinstance(val_template, str) and val_template.startswith("<alias:") and val_template.endswith(">"):
                inner = val_template[7:-1] # e.g., KNOWN_URLS[site]
                if "[" in inner and "]" in inner:
                    dict_name, capture_key = inner.split("[", 1)
                    capture_key = capture_key.rstrip("]")
                    capture_val = match_dict.get(capture_key, "")
                    resolved_args[key] = aliases.get(dict_name, {}).get(capture_val, capture_val)
                else:
                    resolved_args[key] = val_template
            else:
                if isinstance(val_template, str) and val_template in match_dict:
                    resolved_args[key] = match_dict[val_template]
                else:
                    resolved_args[key] = val_template
        return resolved_args


class ConfidenceGate:
    def __init__(self, threshold: float = 0.75):
        self.threshold = threshold

    def evaluate(self, rule: RuleDefinition) -> bool:
        return rule.confidence >= self.threshold


class RuleEngine:
    def __init__(self, loader: RuleLoader, enricher: ContextEnricher) -> None:
        self._loader = loader
        self._alias_resolver = AliasResolver(enricher)
        self._gate = ConfidenceGate(threshold=0.75)

    def process(self, text: str) -> ActionPolicy | None:
        """Evaluates the text against all loaded YAML rules. Returns ActionPolicy if confident match."""
        rules = self._loader.get_rules()
        best_match: ActionPolicy | None = None
        highest_confidence = -1.0

        for rule in rules:
            for regex in rule.regexes:
                match = regex.match(text)
                if match:
                    if self._gate.evaluate(rule) and rule.confidence > highest_confidence:
                        resolved_args = self._alias_resolver.resolve(rule.args_map, match.groupdict(), text)
                        best_match = ActionPolicy(
                            intent=rule.id,
                            tool=rule.tool,
                            args=resolved_args,
                            confidence=rule.confidence,
                            permission_level=rule.permission,
                            requires_confirm=rule.requires_confirm
                        )
                        highest_confidence = rule.confidence
        
        return best_match
