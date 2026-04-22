"""Behavior Learning — optimizes frequent actions based on feedback.

Tracks successful routing decisions and commands to optimize future
responses and automatically adjust confidence thresholds.
"""
from __future__ import annotations

import logging
from collections import defaultdict
import threading

logger = logging.getLogger("Jarvis.BehaviorLearning")

class BehaviorLearning:
    def __init__(self):
        self._success_history: dict[str, int] = defaultdict(int)
        self._failure_history: dict[str, int] = defaultdict(int)
        self._trust_scores: dict[str, float] = defaultdict(lambda: 0.5) # Default 50% trust
        self._action_sequences: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._last_action: str | None = None
        self._lock = threading.Lock()

    def record_feedback(self, text: str, success: bool) -> None:
        """Record success or failure for a given input."""
        # Simple normalization
        normalized = text.strip().lower()
        with self._lock:
            # Update Trust Score
            if success:
                self._trust_scores[normalized] = min(1.0, self._trust_scores[normalized] + 0.1)
                
                # Update sequence prediction if successful
                if self._last_action:
                    self._action_sequences[self._last_action][normalized] += 1
                self._last_action = normalized
            else:
                self._trust_scores[normalized] = max(0.0, self._trust_scores[normalized] - 0.2)
                self._last_action = None
                
        logger.debug("Feedback recorded for: '%s' (Success: %s, Trust: %.2f)", normalized, success, self._trust_scores[normalized])

    def get_confidence_modifier(self, text: str) -> float:
        """Adjust confidence dynamically based on past success rates and trust score."""
        normalized = text.strip().lower()
        with self._lock:
            trust = self._trust_scores[normalized]
            
        # Base modifier off trust score
        if trust > 0.8:
            return 0.15 # Boost confidence
        elif trust < 0.3:
            return -0.2 # Penalize confidence
            
        return 0.0

    def predict_next_action(self, current_action: str) -> str | None:
        """Predict the most likely next action based on historical sequences."""
        normalized = current_action.strip().lower()
        with self._lock:
            transitions = self._action_sequences.get(normalized)
            if not transitions:
                return None
            
            # Find the most frequent following action
            best_action = max(transitions.items(), key=lambda x: x[1])
            if best_action[1] >= 2: # At least observed twice
                return best_action[0]
        return None

learner = BehaviorLearning()
