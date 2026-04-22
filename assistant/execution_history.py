"""Execution History — tracks all actions, their results, and trust impacts.

Maintains an enterprise-level audit log of system modifications.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("Jarvis.ExecutionHistory")

class ExecutionHistory:
    
    def __init__(self, storage_dir: Path | str | None = None) -> None:
        if storage_dir is None:
            storage_dir = Path(__file__).resolve().parents[1] / "memory"
        self._storage_path = Path(storage_dir) / "execution_history.json"
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._history: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self._storage_path.exists():
            try:
                with open(self._storage_path, "r", encoding="utf-8") as f:
                    self._history = json.load(f)
            except Exception as e:
                logger.error("Failed to load execution history: %s", e)

    def _save(self) -> None:
        try:
            with open(self._storage_path, "w", encoding="utf-8") as f:
                json.dump(self._history[-500:], f, indent=2) # Keep last 500
        except Exception as e:
            logger.error("Failed to save execution history: %s", e)

    def log_action(self, task_name: str, args: dict[str, Any], result: str, success: bool, trust_impact: float = 0.0) -> None:
        """Log an executed action into the audit trail."""
        with self._lock:
            record = {
                "timestamp": time.time(),
                "task": task_name,
                "args": args,
                "success": success,
                "result": result[:500], # truncate long outputs
                "trust_impact": trust_impact
            }
            self._history.append(record)
            self._save()

    def get_recent_failures(self, task_name: str, seconds: int = 3600) -> int:
        """Count consecutive failures for a specific task within a time window."""
        with self._lock:
            cutoff = time.time() - seconds
            failures = 0
            # Traverse backwards
            for record in reversed(self._history):
                if record.get("timestamp", 0) < cutoff:
                    break
                if record.get("task") == task_name:
                    if not record.get("success"):
                        failures += 1
                    else:
                        break # Stop counting on the first success
            return failures

    def is_rate_limited(self, task_name: str, args: dict[str, Any]) -> tuple[bool, str]:
        """Check if a specific command signature is being executed too frequently."""
        with self._lock:
            cutoff = time.time() - 60
            recent_count = 0
            
            # Create a simplified signature for the action
            target = str(args.get("command") or args.get("app_name") or "")
            signature = f"{task_name}:{target}"
            
            for record in reversed(self._history):
                if record.get("timestamp", 0) < cutoff:
                    break
                    
                rec_target = str(record.get("args", {}).get("command") or record.get("args", {}).get("app_name") or "")
                rec_signature = f"{record.get('task')}:{rec_target}"
                
                if rec_signature == signature:
                    recent_count += 1
                    
            if recent_count >= 5: # Max 5 identical executions per minute
                return True, f"Rate limit exceeded: Identical task '{signature}' executed {recent_count} times in the last minute."
            return False, ""

# Singleton
execution_history = ExecutionHistory()
