"""Proactive Decision Engine — the final brain of Jarvis.

Decides whether to:
    - AUTO ACT  (very high confidence) — execute without asking
    - SUGGEST   (high confidence) — propose to the user
    - STAY SILENT (low confidence) — do nothing

Refinements:
    - Context awareness: checks system status, active app, time of day
    - Suppresses during active voice sessions, speaking, errors
    - Feedback loop: accepted/rejected feed back into memory graph
    - Single suggestion per evaluation cycle (fatigue prevention)
    - Night mode suppression (configurable quiet hours)

Respects:
    - User interruption
    - Safe mode
    - Privacy guard
    - Voice identity (GUEST mode → no proactive actions)
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("Jarvis.ProactiveEngine")

# ── Decision thresholds ──────────────────────────────────────────────
SUGGEST_THRESHOLD = 0.55
AUTO_ACT_THRESHOLD = 0.85
COOLDOWN_SECONDS = 180              # 3 min between same-action suggestions
CATEGORY_COOLDOWN_SECONDS = 300     # 5 min between same-category suggestions
CHECK_INTERVAL_SEC = 60
QUIET_HOURS = (0, 6)                # Suppress suggestions between midnight and 6am


class Decision(Enum):
    SILENT = "SILENT"
    SUGGEST = "SUGGEST"
    AUTO_ACT = "AUTO_ACT"


@dataclass
class ProactiveAction:
    """An action the engine decided to take or suggest."""
    action_id: str
    label: str
    decision: Decision
    confidence: float
    reason: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "label": self.label,
            "decision": self.decision.value,
            "confidence": self.confidence,
            "reason": self.reason,
        }


class ProactiveEngine:
    """Final decision layer — evaluates predictions and decides on action."""

    def __init__(
        self,
        auto_act_enabled: bool = False,
        on_suggest: Callable[[ProactiveAction], None] | None = None,
        on_auto_act: Callable[[ProactiveAction], None] | None = None,
    ) -> None:
        self._auto_act_enabled = auto_act_enabled
        self._on_suggest = on_suggest
        self._on_auto_act = on_auto_act

        self._safe_mode = False
        self._guest_mode = False
        self._suppressed_until: dict[str, float] = {}
        self._category_suppressed: dict[str, float] = {}
        self._action_history: list[ProactiveAction] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

        # Track how many suggestions were emitted this session
        # to enforce global rate limiting
        self._session_suggest_count = 0
        self._last_suggest_time = 0.0

        try:
            from assistant.event_bus import bus
            bus.subscribe("system.safe_mode_engaged", self._on_safe_mode)
            bus.subscribe("voice.guest_detected", self._on_guest_detected)
            bus.subscribe("voice.identity_verified", self._on_user_verified)
        except Exception as e:
            logger.error("Failed to subscribe ProactiveEngine to event bus: %s", e)

    # ── Configuration ────────────────────────────────────────────────

    def set_auto_act(self, enabled: bool) -> None:
        self._auto_act_enabled = enabled
        logger.info("Auto-act %s", "enabled" if enabled else "disabled")

    def set_guest_mode(self, guest: bool) -> None:
        self._guest_mode = guest
        if guest:
            logger.info("Guest mode: proactive actions disabled")

    def set_callbacks(
        self,
        on_suggest: Callable[[ProactiveAction], None] | None = None,
        on_auto_act: Callable[[ProactiveAction], None] | None = None,
    ) -> None:
        self._on_suggest = on_suggest
        self._on_auto_act = on_auto_act

    # ── Background loop ──────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._decision_loop, daemon=True, name="ProactiveEngine")
        self._thread.start()
        logger.info("Proactive engine started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)

    def _decision_loop(self) -> None:
        time.sleep(15.0 + random.uniform(0.0, 10.0))
        while self._running:
            try:
                self._evaluate_predictions()
            except Exception as e:
                logger.error("Proactive evaluation failed: %s", e)
            time.sleep(CHECK_INTERVAL_SEC + random.uniform(-15.0, 15.0))

    # ── Context gates ────────────────────────────────────────────────

    def _is_context_inappropriate(self) -> bool:
        """Check if the current context makes suggestions inappropriate."""
        # 1. Safe mode / guest mode
        if self._safe_mode or self._guest_mode:
            return True

        # 2. Quiet hours (night)
        hour = datetime.now().hour
        if QUIET_HOURS[0] <= hour < QUIET_HOURS[1]:
            return True

        # 3. System is actively processing a command
        try:
            from assistant.global_state import global_state
            status = global_state.status
            if status in ("SPEAKING", "THINKING", "RECOGNIZING", "LISTENING", "TYPING", "PROCESSING", "EXECUTING", "RESPONDING"):
                return True
        except Exception as e:
            logger.debug("Global state unavailable for proactive check: %s", e)
            
        # 4. User Do Not Disturb mode
        try:
            from assistant.intel_prefs import intel_prefs
            if intel_prefs.dnd_active:
                return True
        except Exception as e:
            logger.debug("Intel prefs unavailable for proactive check: %s", e)

        # 5. Rate limit: don't spam suggestions faster than every 2 min
        if time.time() - self._last_suggest_time < 120:
            return True
            
        # 6. Environmental context (auto-DND conditions)
        try:
            from assistant.predictive_engine import predictor
            ctx = predictor.context_snapshot
            if ctx.get("screen_locked"):
                return True
            if ctx.get("fullscreen"):
                return True
            if ctx.get("meeting_active"):
                return True
        except Exception as e:
            logger.debug("Predictor context unavailable for proactive check: %s", e)

        return False

    def _is_action_on_cooldown(self, action_id: str) -> bool:
        now = time.time()
        return now < self._suppressed_until.get(action_id, 0)

    def _is_category_on_cooldown(self, action_id: str) -> bool:
        now = time.time()
        category = self._extract_category(action_id)
        return now < self._category_suppressed.get(category, 0)

    @staticmethod
    def _extract_category(action_id: str) -> str:
        if action_id.startswith("app:"):
            return action_id[4:]
        if action_id.startswith("action:"):
            core = action_id[7:]
            for prefix in ("open ", "launch ", "start ", "check ", "close "):
                if core.startswith(prefix):
                    return core[len(prefix):]
            return core
        return action_id

    # -- Core decision logic ---------------------------------------------------

    def _evaluate_predictions(self) -> None:
        """Pull predictions from PredictiveEngine, apply context gates,
        preference filters, and personality frequency, then emit at most
        1 suggestion per cycle."""
        if self._is_context_inappropriate():
            return

        try:
            from assistant.predictive_engine import predictor
        except ImportError:
            return

        # Load user preferences for filtering
        prefs = None
        try:
            from assistant.intel_prefs import intel_prefs
            prefs = intel_prefs
        except Exception as e:
            logger.debug("Intel prefs unavailable for threshold adjustment: %s", e)

        # Apply personality frequency profile to thresholds
        freq_profile = prefs.frequency_profile if prefs else {}
        effective_suggest = SUGGEST_THRESHOLD + freq_profile.get("suggest_threshold_offset", 0.0)
        effective_suggest = max(0.30, min(0.80, effective_suggest))
        cooldown_mult = freq_profile.get("cooldown_multiplier", 1.0)

        predictions = predictor.get_predictions()
        now = time.time()
        emitted = False

        for pred in predictions:
            if emitted:
                break

            if self._is_action_on_cooldown(pred.action_id):
                continue
            if self._is_category_on_cooldown(pred.action_id):
                continue

            # User preference: category disabled?
            if prefs:
                category = self._extract_category(pred.action_id)
                if not prefs.is_category_enabled(category):
                    continue
                # Source disabled? (e.g. exploration off)
                if not prefs.is_source_enabled(pred.source):
                    continue

            decision = self._decide_with_threshold(
                pred.confidence, effective_suggest)
            if decision == Decision.SILENT:
                continue

            action = ProactiveAction(
                action_id=pred.action_id,
                label=pred.label,
                decision=decision,
                confidence=pred.confidence,
                reason=pred.reason,
                timestamp=now,
            )

            # Apply cooldowns (scaled by personality frequency)
            self._suppressed_until[pred.action_id] = (
                now + COOLDOWN_SECONDS * cooldown_mult)
            cat = self._extract_category(pred.action_id)
            self._category_suppressed[cat] = (
                now + CATEGORY_COOLDOWN_SECONDS * cooldown_mult)

            with self._lock:
                self._action_history.append(action)
                if len(self._action_history) > 100:
                    self._action_history = self._action_history[-50:]

            if decision == Decision.AUTO_ACT and self._auto_act_enabled:
                logger.info("AUTO ACT: %s (conf=%.2f)", action.label, action.confidence)
                self._dispatch_auto_act(action)
            elif decision == Decision.SUGGEST:
                logger.info("SUGGEST: %s (conf=%.2f)", action.label, action.confidence)
                self._dispatch_suggestion(action)

            self._last_suggest_time = now
            self._session_suggest_count += 1
            emitted = True

    def _decide(self, confidence: float) -> Decision:
        if confidence >= AUTO_ACT_THRESHOLD:
            return Decision.AUTO_ACT
        elif confidence >= SUGGEST_THRESHOLD:
            return Decision.SUGGEST
        return Decision.SILENT

    def _decide_with_threshold(self, confidence: float,
                                suggest_threshold: float) -> Decision:
        """Like _decide but with a custom suggest threshold from personality."""
        if confidence >= AUTO_ACT_THRESHOLD:
            return Decision.AUTO_ACT
        elif confidence >= suggest_threshold:
            return Decision.SUGGEST
        return Decision.SILENT

    # ── On-demand evaluation ─────────────────────────────────────────

    def evaluate_immediate(self, action_id: str, label: str,
                           confidence: float, reason: str) -> ProactiveAction:
        decision = self._decide(confidence)

        if self._safe_mode or self._guest_mode:
            decision = Decision.SILENT

        # Context check: suppress if system busy
        if self._is_context_inappropriate():
            decision = Decision.SILENT

        action = ProactiveAction(
            action_id=action_id,
            label=label,
            decision=decision,
            confidence=confidence,
            reason=reason,
            timestamp=time.time(),
        )

        with self._lock:
            self._action_history.append(action)

        return action

    # ── Dispatch ─────────────────────────────────────────────────────

    def _dispatch_suggestion(self, action: ProactiveAction) -> None:
        if self._on_suggest:
            try:
                self._on_suggest(action)
            except Exception as e:
                logger.error("Suggestion callback failed: %s", e)

        try:
            from assistant.event_bus import bus
            bus.publish_async("proactive.suggestion", action.to_dict())
        except Exception as e:
            logger.error("Failed to publish proactive suggestion to bus: %s", e)

    def _dispatch_auto_act(self, action: ProactiveAction) -> None:
        if self._on_auto_act:
            try:
                self._on_auto_act(action)
            except Exception as e:
                logger.error("Auto-act callback failed: %s", e)

        try:
            from assistant.event_bus import bus
            bus.publish_async("proactive.auto_act", action.to_dict())
        except Exception as e:
            logger.error("Failed to publish proactive auto-act to bus: %s", e)

    # ── User feedback ────────────────────────────────────────────────

    def accept_suggestion(self, action_id: str) -> None:
        """User accepted → reinforce in predictive engine + graph."""
        try:
            from assistant.predictive_engine import predictor
            predictor.accept_prediction(action_id)
        except Exception as e:
            logger.error("Failed to record suggestion acceptance in predictor: %s", e)

        # Also boost directly in memory graph
        try:
            from assistant.memory_graph import graph
            time_id = f"time:{datetime.now().hour:02d}"
            graph.boost_edge(action_id, time_id, delta=2.0)
        except Exception as e:
            logger.error("Failed to boost edge in memory graph: %s", e)

        logger.info("Suggestion accepted: %s", action_id)

    def reject_suggestion(self, action_id: str) -> None:
        """User rejected -> penalize + cooldown."""
        now = time.time()
        
        # Stability clamp: do not indefinitely compound cooldowns if rapidly rejected
        # Max cooldown is 5x normal. If already beyond 2x, just cap it.
        current_cooldown = self._suppressed_until.get(action_id, 0)
        if current_cooldown < now + COOLDOWN_SECONDS * 2:
            self._suppressed_until[action_id] = now + COOLDOWN_SECONDS * 5
            
        category = self._extract_category(action_id)
        current_cat_cooldown = self._category_suppressed.get(category, 0)
        if current_cat_cooldown < now + CATEGORY_COOLDOWN_SECONDS * 2:
            self._category_suppressed[category] = now + CATEGORY_COOLDOWN_SECONDS * 3

        try:
            from assistant.predictive_engine import predictor
            predictor.reject_prediction(action_id)
        except Exception as e:
            logger.error("Failed to record suggestion rejection in predictor: %s", e)

        # Penalize in memory graph
        try:
            from assistant.memory_graph import graph
            time_id = f"time:{datetime.now().hour:02d}"
            graph.penalize_edge(action_id, time_id, delta=3.0)
        except Exception as e:
            logger.error("Failed to penalize edge in memory graph: %s", e)

        logger.info("Suggestion rejected: %s", action_id)

    # ── Event handlers ───────────────────────────────────────────────

    def _on_safe_mode(self, **kwargs) -> None:
        self._safe_mode = True
        logger.warning("Safe mode engaged — proactive actions suspended")

    def _on_guest_detected(self, **kwargs) -> None:
        self._guest_mode = True
        logger.info("Guest detected — proactive actions disabled")

    def _on_user_verified(self, **kwargs) -> None:
        self._guest_mode = False
        logger.info("User verified — proactive actions re-enabled")

    def disengage_safe_mode(self) -> None:
        self._safe_mode = False
        logger.info("Safe mode disengaged — proactive actions resumed")

    # ── History & stats ──────────────────────────────────────────────

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [a.to_dict() for a in self._action_history[-limit:]]

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "session_suggestions": self._session_suggest_count,
            "safe_mode": self._safe_mode,
            "guest_mode": self._guest_mode,
            "auto_act_enabled": self._auto_act_enabled,
            "active_cooldowns": len([
                k for k, v in self._suppressed_until.items()
                if v > time.time()
            ]),
        }


# ── Singleton ────────────────────────────────────────────────────────
proactive = ProactiveEngine()
