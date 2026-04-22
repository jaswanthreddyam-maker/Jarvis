"""Trust Manager — tracks user trust levels for different task categories.

High trust -> auto-execute
Medium trust -> ask confirmation
Critical tasks -> always ask confirmation

Implements quality-weighted updates and trust decay over time.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("Jarvis.TrustManager")

class TrustManager:
    """Manages trust levels per task type to build automation confidence over time."""

    # Trust threshold to skip confirmation
    AUTO_EXECUTE_THRESHOLD = 0.8
    
    # Types that always require confirmation regardless of score
    CRITICAL_TASK_TYPES = {"system_config", "file_deletion", "unknown_script"}

    # Decay rate: 0.05 per day of inactivity
    DECAY_RATE_PER_DAY = 0.05

    def __init__(self, storage_dir: Path | str | None = None) -> None:
        if storage_dir is None:
            storage_dir = Path(__file__).resolve().parents[1] / "memory"
        self._storage_path = Path(storage_dir) / "trust_scores.json"
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Schema: { task_type: {"score": float, "last_used": float} }
        self._trust_data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._init_defaults()
        self._load()

    def _init_defaults(self) -> None:
        now = time.time()
        defaults = {
            "open_app": 0.9,
            "web_search": 0.9,
            "install_app": 0.4,
            "run_command": 0.3,
            "system_config": 0.1,
            "file_deletion": 0.0
        }
        for k, v in defaults.items():
            self._trust_data[k] = {"score": v, "last_used": now}

    def _load(self) -> None:
        if self._storage_path.exists():
            try:
                with open(self._storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                now = time.time()
                for k, v in data.items():
                    if isinstance(v, float): # Migration from old format
                        self._trust_data[k] = {"score": v, "last_used": now}
                    else:
                        self._trust_data[k] = v
            except Exception as e:
                logger.error("Failed to load trust scores: %s", e)

    def _save(self) -> None:
        try:
            with open(self._storage_path, "w", encoding="utf-8") as f:
                json.dump(self._trust_data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save trust scores: %s", e)

    def _apply_decay(self, task_type: str) -> float:
        """Apply time-based decay to trust score."""
        if task_type not in self._trust_data:
            return 0.2
            
        data = self._trust_data[task_type]
        score = data["score"]
        last_used = data["last_used"]
        now = time.time()
        
        days_passed = (now - last_used) / 86400.0
        if days_passed > 1.0:
            decay = int(days_passed) * self.DECAY_RATE_PER_DAY
            score = max(0.0, score - decay)
            # We don't update last_used here to allow continuous decay until next use
            self._trust_data[task_type]["score"] = score
            
        return score

    def is_trusted(self, task_type: str) -> bool:
        """Check if a task type can be auto-executed."""
        if task_type in self.CRITICAL_TASK_TYPES:
            return False
            
        with self._lock:
            score = self._apply_decay(task_type)
            return score >= self.AUTO_EXECUTE_THRESHOLD

    def get_trust(self, task_type: str) -> float:
        with self._lock:
            return self._apply_decay(task_type)

    def record_success(self, task_type: str, quality_score: float = 1.0) -> None:
        """User approved/succeeded -> increase trust weighted by quality.
        Quality 1.0 -> standard success (+0.1)
        Quality 0.5 -> partial success (+0.05)
        """
        if task_type in self.CRITICAL_TASK_TYPES:
            return
            
        with self._lock:
            current = self._apply_decay(task_type)
            increase = 0.1 * quality_score
            new_score = min(1.0, current + increase)
            
            self._trust_data[task_type] = {"score": new_score, "last_used": time.time()}
            self._save()
            logger.info("Trust increased for %s (Quality %.1f) -> %.2f", task_type, quality_score, new_score)

    def record_cancellation(self, task_type: str) -> None:
        """User denied -> decrease trust heavily."""
        if task_type in self.CRITICAL_TASK_TYPES:
            return
            
        with self._lock:
            current = self._apply_decay(task_type)
            new_score = max(0.0, current - 0.3)
            
            self._trust_data[task_type] = {"score": new_score, "last_used": time.time()}
            self._save()
            logger.warning("Trust decreased for %s -> %.2f", task_type, new_score)

# Singleton
trust_manager = TrustManager()
