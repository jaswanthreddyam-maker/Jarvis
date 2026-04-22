"""Predictive Engine - anticipates user needs based on patterns.

Analyzes the Memory Graph + current time context to predict
what the user is likely to want next.

v4 refinements:
    - Threshold decay: drifts back toward BASE (0.50) over time
    - Cold start: surfaces low-confidence intro suggestions for new users
    - Penalty decay: rejections fade in memory graph via boost half-life
    - Explainability: rich structured reasons on every suggestion
    - Biased exploration: prioritizes weak patterns by composite score
    - Multi-context: active app, system load, user idle detection
    - User preferences: per-category enable/disable
    - Personality: tone + frequency profile applied to suggestions
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("Jarvis.PredictiveEngine")

# -- Tunable thresholds -------------------------------------------------------
BASE_MIN_CONFIDENCE = 0.50
MIN_PREDICTION_WEIGHT = 2.5
PREDICTION_INTERVAL_SEC = 300
MAX_PREDICTIONS_PER_CYCLE = 1
CATEGORY_COOLDOWN_SEC = 600

# -- Adaptive threshold bounds ------------------------------------------------
_THRESHOLD_FLOOR = 0.35
_THRESHOLD_CEILING = 0.80
_THRESHOLD_ACCEPT_SHIFT = -0.02
_THRESHOLD_REJECT_SHIFT = +0.04
_THRESHOLD_DECAY_RATE = 0.005

# -- Exploration --------------------------------------------------------------
_EXPLORE_PROBABILITY = 0.10
_EXPLORE_MIN_CONFIDENCE = 0.20
_EXPLORE_COOLDOWN_SEC = 1800

# -- Cold-start ---------------------------------------------------------------
_COLD_START_EDGE_THRESHOLD = 8
_COLD_START_CONFIDENCE = 0.30


# -- Explainability -----------------------------------------------------------
@dataclass
class ExplainableReason:
    """Structured reason attached to every prediction."""
    pattern_type: str
    detail: str
    edge_weight: float = 0.0
    edge_count: int = 0
    recency_days: float = 0.0

    def __str__(self) -> str:
        return self.detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_type": self.pattern_type,
            "detail": self.detail,
            "edge_weight": round(self.edge_weight, 2),
            "edge_count": self.edge_count,
            "recency_days": round(self.recency_days, 1),
        }


@dataclass
class Prediction:
    """A predicted user action with confidence."""
    action_id: str
    label: str
    confidence: float
    reason: str
    source: str = "time"
    category: str = ""
    explanation: ExplainableReason | None = None
    is_exploration: bool = False
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "action_id": self.action_id,
            "label": self.label,
            "confidence": self.confidence,
            "reason": self.reason,
            "source": self.source,
            "category": self.category,
            "is_exploration": self.is_exploration,
        }
        if self.explanation:
            d["explanation"] = self.explanation.to_dict()
        return d


class PredictiveEngine:
    """Generates contextual predictions from the memory graph."""

    def __init__(self, memory_graph=None) -> None:
        self._graph = memory_graph
        self._predictions: list[Prediction] = []
        self._rejected: set[str] = set()
        self._accepted: set[str] = set()

        self._min_confidence = BASE_MIN_CONFIDENCE
        self._category_cooldowns: dict[str, float] = {}

        self._accept_count = 0
        self._reject_count = 0
        self._last_threshold_decay = time.time()
        self._last_threshold_shift = 0.0
        self._last_explore_time = 0.0

        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def _get_graph(self):
        if self._graph is None:
            from assistant.memory_graph import graph
            self._graph = graph
        return self._graph

    def _get_prefs(self):
        """Lazy-load intelligence preferences."""
        try:
            from assistant.intel_prefs import intel_prefs
            return intel_prefs
        except Exception:
            return None

    # -- Background loop ------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._prediction_loop, daemon=True, name="PredictiveEngine")
        self._thread.start()
        logger.info("Predictive engine started (threshold=%.2f)", self._min_confidence)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)

    def _prediction_loop(self) -> None:
        time.sleep(10.0)
        while self._running:
            try:
                self._decay_threshold()
                self._generate_predictions()
            except Exception as e:
                logger.error("Prediction generation failed: %s", e)
            time.sleep(PREDICTION_INTERVAL_SEC)

    # -- 1. Threshold decay ---------------------------------------------------

    def _decay_threshold(self) -> None:
        """Slowly drift _min_confidence back toward BASE_MIN_CONFIDENCE."""
        if abs(self._min_confidence - BASE_MIN_CONFIDENCE) < 0.005:
            return
        direction = 1.0 if self._min_confidence < BASE_MIN_CONFIDENCE else -1.0
        self._min_confidence += direction * _THRESHOLD_DECAY_RATE
        self._min_confidence = max(
            _THRESHOLD_FLOOR,
            min(_THRESHOLD_CEILING, self._min_confidence),
        )
        logger.debug("Threshold decayed to %.3f", self._min_confidence)

    # -- Multi-context awareness ----------------------------------------------

    def _should_suppress(self) -> bool:
        """Multi-context suppression: system status + CPU load."""
        try:
            from assistant.global_state import global_state
            if global_state.status in ("SPEAKING", "THINKING", "RECOGNIZING", "LISTENING", "TYPING", "PROCESSING", "EXECUTING", "RESPONDING"):
                return True
        except Exception as e:
            logger.debug("Global state unavailable for suppression check: %s", e)

        if self._is_system_under_load():
            return True

        return False

    def _is_system_under_load(self) -> bool:
        """Check if CPU is >85%."""
        try:
            import psutil
            return psutil.cpu_percent(interval=0) > 85.0
        except Exception:
            return False

    def _get_active_app(self) -> str | None:
        """Return the currently focused window's app name (Windows)."""
        try:
            import ctypes
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.lower()
            for sep in (" - ", " | "):
                if sep in title:
                    return title.rsplit(sep, 1)[-1].strip()
            return title.strip() or None
        except Exception:
            return None

    def _get_idle_time_ms(self) -> int:
        """Get the number of milliseconds since the last user input."""
        try:
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))  # type: ignore
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime  # type: ignore
            return millis
        except Exception:
            return 0

    def _is_user_idle(self) -> bool:
        """Check if user hasn't touched mouse/keyboard for >5 min."""
        return self._get_idle_time_ms() > 300_000
        
    def _is_screen_locked(self) -> bool:
        """Detect if Windows workstation is locked."""
        try:
            import ctypes
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            # When locked, GetForegroundWindow often returns 0
            # Or we can check if it's the lock screen app
            hwnd = user32.GetForegroundWindow()
            if hwnd == 0:
                return True
            return False
        except Exception:
            return False
            
    def _is_fullscreen_app_active(self) -> bool:
        """Detect if the active window is fullscreen (e.g., game, video)."""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
                
            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            
            rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            screen_width = user32.GetSystemMetrics(0)
            screen_height = user32.GetSystemMetrics(1)
            
            # Allow 1-pixel margin for borderless windows
            return (rect.right - rect.left >= screen_width) and (rect.bottom - rect.top >= screen_height)
        except Exception:
            return False

    def _is_meeting_active(self) -> bool:
        """Detect if user is in an active video meeting."""
        active_app = self._get_active_app()
        if not active_app:
            return False
        active_app = active_app.lower()
        return any(x in active_app for x in ["zoom meeting", "microsoft teams", "google meet", "webex"])

    def _get_context_snapshot(self) -> dict[str, Any]:
        """Gather current environmental context."""
        idle_ms = self._get_idle_time_ms()
        return {
            "active_app": self._get_active_app(),
            "user_idle": idle_ms > 300_000,
            "idle_ms": idle_ms,
            "system_loaded": self._is_system_under_load(),
            "screen_locked": self._is_screen_locked(),
            "fullscreen": self._is_fullscreen_app_active(),
            "meeting_active": self._is_meeting_active(),
            "hour": datetime.now().hour,
        }

    def _is_category_on_cooldown(self, category: str) -> bool:
        return time.time() < self._category_cooldowns.get(category, 0)

    def _set_category_cooldown(self, category: str) -> None:
        self._category_cooldowns[category] = time.time() + CATEGORY_COOLDOWN_SEC

    # -- Cold start -----------------------------------------------------------

    def _is_cold_start(self) -> bool:
        graph = self._get_graph()
        stats = graph.stats()
        return stats.get("total_edges", 0) < _COLD_START_EDGE_THRESHOLD

    def _generate_cold_start_suggestions(self) -> list[Prediction]:
        now = datetime.now()
        hour = now.hour
        suggestions: list[Prediction] = []

        if 6 <= hour < 10:
            suggestions.append(Prediction(
                action_id="action:check schedule",
                label="Check today's schedule",
                confidence=_COLD_START_CONFIDENCE,
                reason="Good morning! Want to check today's schedule?",
                source="cold_start", category="schedule",
                explanation=ExplainableReason(
                    pattern_type="cold_start",
                    detail="Morning suggestion for new users",
                ),
            ))
        elif 12 <= hour < 14:
            suggestions.append(Prediction(
                action_id="action:check news",
                label="Check the latest news",
                confidence=_COLD_START_CONFIDENCE,
                reason="Lunch break? Catch up on news.",
                source="cold_start", category="news",
                explanation=ExplainableReason(
                    pattern_type="cold_start",
                    detail="Afternoon suggestion for new users",
                ),
            ))
        elif 18 <= hour < 22:
            suggestions.append(Prediction(
                action_id="action:play music",
                label="Play some music",
                confidence=_COLD_START_CONFIDENCE,
                reason="Winding down? I can play music for you.",
                source="cold_start", category="music",
                explanation=ExplainableReason(
                    pattern_type="cold_start",
                    detail="Evening suggestion for new users",
                ),
            ))

        return suggestions

    # -- Explainability -------------------------------------------------------

    def _build_explanation(self, action_id: str, source: str,
                           composite_score: float,
                           hour: int) -> ExplainableReason:
        graph = self._get_graph()
        edge_data = graph.get_edge_raw(action_id, f"time:{hour:02d}")
        count = edge_data.get("count", 0) if edge_data else 0
        last_updated = edge_data.get("last_updated", time.time()) if edge_data else time.time()
        recency = (time.time() - last_updated) / 86400.0

        if source == "time":
            detail = (
                f"Observed {count} time(s) around {hour}:00. "
                f"Last seen {recency:.1f} days ago. "
                f"Composite strength: {composite_score:.1f}."
            )
        elif source == "frequency":
            detail = (
                f"Used {count} time(s) total, active at this hour. "
                f"Last activity {recency:.1f} days ago."
            )
        elif source == "sequence":
            detail = (
                f"Follows a previous action {count} time(s). "
                f"Pattern strength: {composite_score:.1f}."
            )
        elif source == "explore":
            detail = (
                f"Exploratory: weak signal with {count} occurrence(s), "
                f"score {composite_score:.1f}. Biased toward strongest weak pattern."
            )
        else:
            detail = f"Score: {composite_score:.1f}, count: {count}."

        return ExplainableReason(
            pattern_type=source, detail=detail,
            edge_weight=composite_score, edge_count=count,
            recency_days=recency,
        )

    def _build_reason_string(self, source: str, hour: int,
                              count: int, last_action: str = "") -> str:
        if source == "time":
            if count >= 10:
                return f"Strong habit: you do this almost every day around {hour}:00"
            elif count >= 5:
                return f"Regular pattern: you often do this around {hour}:00"
            else:
                return f"Emerging pattern: you've done this {count}x around {hour}:00"
        elif source == "frequency":
            return f"One of your most-used apps at this hour ({count} uses)"
        elif source == "sequence":
            return f"You often do this after '{last_action[:25]}'"
        elif source == "explore":
            return f"Trying something new based on a weak signal ({count} occurrence(s))"
        elif source == "cold_start":
            return "Suggested for new users at this time of day"
        return f"Based on {count} observations"

    # -- Core prediction generation -------------------------------------------

    def _generate_predictions(self) -> None:
        if self._should_suppress():
            return

        prefs = self._get_prefs()

        # Apply frequency profile offsets from personality
        effective_threshold = self._min_confidence
        freq_profile = prefs.frequency_profile if prefs else {}
        effective_threshold += freq_profile.get("threshold_offset", 0.0)
        effective_threshold = max(_THRESHOLD_FLOOR, min(_THRESHOLD_CEILING, effective_threshold))
        cooldown_mult = freq_profile.get("cooldown_multiplier", 1.0)

        # Cold start path
        if self._is_cold_start():
            if prefs and not prefs.is_source_enabled("cold_start"):
                return
            cold = self._generate_cold_start_suggestions()
            if prefs and cold:
                for s in cold:
                    s.reason = prefs.format_suggestion(s.label, s.source, s.category)
            with self._lock:
                self._predictions = cold[:MAX_PREDICTIONS_PER_CYCLE]
            if self._predictions:
                logger.info("Cold-start suggestion: %s", self._predictions[0].label)
            return

        graph = self._get_graph()
        now = datetime.now()
        hour = now.hour
        context = self._get_context_snapshot()
        candidates: list[Prediction] = []

        # -- Time-based -------------------------------------------------------
        habits = graph.get_habits_at_hour(hour)
        for action_id, composite_score in habits[:5]:
            if composite_score < MIN_PREDICTION_WEIGHT:
                continue
            if action_id in self._rejected:
                continue
            category = self._extract_category(action_id)
            if self._is_category_on_cooldown(category):
                continue

            # Preference filter
            if prefs and not prefs.is_category_enabled(category):
                continue

            # Context: user already has this app focused
            active_app = context.get("active_app", "") or ""
            if active_app and category in active_app:
                continue

            # Context weighting: adjust confidence
            context_mult = 1.0
            if context.get("user_idle"):
                context_mult *= 0.8  # Less confident if user is not active
                
            # Emotional Tone / Stress tracking: high activity = wait for a gap
            # Natural interaction gap: idle between 5s and 60s
            idle_ms = context.get("idle_ms", 0)
            if idle_ms < 5000:
                # User is actively typing/working -> suppress confidence
                context_mult *= 0.5 
            elif 5000 <= idle_ms < 60000:
                # User paused -> good time to suggest
                context_mult *= 1.2
            
            label = self._humanize_action(action_id)
            base_conf = min(1.0, math.log1p(composite_score) / math.log1p(15.0))
            confidence = base_conf * context_mult
            # Cap confidence to not exceed 1.0
            confidence = min(1.0, confidence)
            explanation = self._build_explanation(action_id, "time", composite_score, hour)

            if confidence >= effective_threshold:
                reason = self._build_reason_string("time", hour, explanation.edge_count)
                if prefs:
                    reason = prefs.format_suggestion(label, "time", category)
                candidates.append(Prediction(
                    action_id=action_id, label=label,
                    confidence=confidence, reason=reason,
                    source="time", category=category,
                    explanation=explanation,
                ))

        # -- Frequency-based --------------------------------------------------
        freq_apps = graph.get_frequent_apps(min_weight=MIN_PREDICTION_WEIGHT)
        for app_id, composite_score in freq_apps[:3]:
            if app_id in self._rejected:
                continue
            category = self._extract_category(app_id)
            if self._is_category_on_cooldown(category):
                continue
            if prefs and not prefs.is_category_enabled(category):
                continue

            active_app = context.get("active_app", "") or ""
            if active_app and category in active_app:
                continue

            time_edges = graph.query_related(app_id, relationship="used_at",
                                              use_composite=True)
            time_match = any(tid == f"time:{hour:02d}" for tid, _, _ in time_edges)
            if time_match:
                # Context weighting
                context_mult = 1.0
                if context.get("user_idle"):
                    context_mult *= 0.8
                    
                idle_ms = context.get("idle_ms", 0)
                if idle_ms < 5000:
                    context_mult *= 0.5
                elif 5000 <= idle_ms < 60000:
                    context_mult *= 1.2

                label = self._humanize_action(app_id)
                base_conf = min(1.0, math.log1p(composite_score) / math.log1p(20.0))
                confidence = min(1.0, base_conf * context_mult)
                explanation = self._build_explanation(
                    app_id, "frequency", composite_score, hour)

                if confidence >= effective_threshold:
                    reason = self._build_reason_string(
                        "frequency", hour, explanation.edge_count)
                    if prefs:
                        reason = prefs.format_suggestion(label, "frequency", category)
                    candidates.append(Prediction(
                        action_id=app_id, label=label,
                        confidence=confidence, reason=reason,
                        source="frequency", category=category,
                        explanation=explanation,
                    ))

        # -- Biased exploration -----------------------------------------------
        if prefs is None or prefs.is_source_enabled("explore"):
            explore_prob = freq_profile.get("explore_probability", _EXPLORE_PROBABILITY)
            explore_pred = self._maybe_explore(hour, candidates, explore_prob, prefs)
            if explore_pred:
                candidates.append(explore_pred)

        # Deduplicate by category
        seen: set[str] = set()
        unique: list[Prediction] = []
        for p in sorted(candidates, key=lambda x: x.confidence, reverse=True):
            if p.category not in seen:
                seen.add(p.category)
                unique.append(p)

        with self._lock:
            self._predictions = unique[:MAX_PREDICTIONS_PER_CYCLE]

        if self._predictions:
            p = self._predictions[0]
            logger.info(
                "Prediction [threshold=%.2f]: %s (conf=%.2f, src=%s%s)",
                self._min_confidence, p.label, p.confidence, p.source,
                ", EXPLORE" if p.is_exploration else "",
            )

    # -- Biased exploration ---------------------------------------------------

    def _maybe_explore(self, hour: int,
                       existing: list[Prediction],
                       explore_prob: float = _EXPLORE_PROBABILITY,
                       prefs=None) -> Prediction | None:
        """Surface a below-threshold suggestion, biased toward the
        strongest weak signals rather than random selection.

        All eligible weak candidates are collected and sorted by
        composite_score descending -- the most promising weak pattern
        gets surfaced first instead of arbitrary iteration order."""
        now = time.time()

        if now - self._last_explore_time < _EXPLORE_COOLDOWN_SEC:
            return None
        if any(p.confidence >= self._min_confidence for p in existing):
            return None
        if random.random() > explore_prob:
            return None

        graph = self._get_graph()
        habits = graph.get_habits_at_hour(hour)

        # Collect all eligible weak candidates
        weak_candidates: list[tuple[str, float]] = []
        existing_ids = {p.action_id for p in existing}

        for action_id, composite_score in habits:
            if action_id in self._rejected:
                continue
            if action_id in existing_ids:
                continue
            category = self._extract_category(action_id)
            if self._is_category_on_cooldown(category):
                continue
            if prefs and not prefs.is_category_enabled(category):
                continue

            confidence = min(1.0, math.log1p(composite_score) / math.log1p(15.0))
            if confidence < _EXPLORE_MIN_CONFIDENCE:
                continue
            if confidence >= self._min_confidence:
                continue  # Normal prediction, not exploration

            weak_candidates.append((action_id, composite_score))

        if not weak_candidates:
            return None

        # Biased: sort by score descending, pick strongest weak pattern
        weak_candidates.sort(key=lambda x: x[1], reverse=True)
        action_id, composite_score = weak_candidates[0]

        category = self._extract_category(action_id)
        label = self._humanize_action(action_id)
        confidence = min(1.0, math.log1p(composite_score) / math.log1p(15.0))
        explanation = self._build_explanation(
            action_id, "explore", composite_score, hour)
        reason = self._build_reason_string("explore", hour, explanation.edge_count)
        if prefs:
            reason = prefs.format_suggestion(label, "explore", category)

        self._last_explore_time = now
        logger.info("Biased exploration: %s (score=%.2f, conf=%.2f, of %d weak)",
                    label, composite_score, confidence, len(weak_candidates))

        return Prediction(
            action_id=action_id, label=label,
            confidence=confidence, reason=reason,
            source="explore", category=category,
            explanation=explanation, is_exploration=True,
        )

    # -- On-demand ------------------------------------------------------------

    def predict_now(self) -> list[Prediction]:
        self._generate_predictions()
        return self.get_predictions()

    def get_predictions(self) -> list[Prediction]:
        with self._lock:
            return list(self._predictions)

    def predict_for_context(self, last_action: str) -> Prediction | None:
        if self._should_suppress():
            return None

        prefs = self._get_prefs()
        graph = self._get_graph()
        action_id = f"action:{last_action.strip().lower()[:80]}"
        hour = datetime.now().hour

        related = graph.query_related(action_id, use_composite=True)
        for target_id, score, rel in related:
            if target_id.startswith(("action:", "app:")) and score >= MIN_PREDICTION_WEIGHT:
                category = self._extract_category(target_id)
                if self._is_category_on_cooldown(category):
                    continue
                if prefs and not prefs.is_category_enabled(category):
                    continue

                label = self._humanize_action(target_id)
                confidence = min(1.0, math.log1p(score) / math.log1p(12.0))
                if confidence >= self._min_confidence:
                    explanation = self._build_explanation(
                        target_id, "sequence", score, hour)
                    reason = self._build_reason_string(
                        "sequence", hour, explanation.edge_count,
                        last_action=last_action)
                    if prefs:
                        reason = prefs.format_suggestion(label, "sequence", category)
                    return Prediction(
                        action_id=target_id, label=label,
                        confidence=confidence, reason=reason,
                        source="sequence", category=category,
                        explanation=explanation,
                    )
        return None

    # -- Feedback learning ----------------------------------------------------

    def accept_prediction(self, action_id: str) -> None:
        with self._lock:
            self._accepted.add(action_id)
            self._rejected.discard(action_id)
            self._accept_count += 1

        graph = self._get_graph()
        time_id = f"time:{datetime.now().hour:02d}"
        graph.boost_edge(action_id, time_id, delta=2.0)
        if action_id.startswith("app:"):
            graph.boost_edge("user:primary", action_id, relationship="uses", delta=1.5)

        self._adjust_threshold(_THRESHOLD_ACCEPT_SHIFT)
        logger.info("Accepted: %s (threshold->%.2f)", action_id, self._min_confidence)
        
        prefs = self._get_prefs()
        if prefs:
            prefs.record_accept()

    def reject_prediction(self, action_id: str) -> None:
        with self._lock:
            self._rejected.add(action_id)
            self._reject_count += 1

        graph = self._get_graph()
        time_id = f"time:{datetime.now().hour:02d}"
        graph.penalize_edge(action_id, time_id, delta=3.0)
        if action_id.startswith("app:"):
            graph.penalize_edge("user:primary", action_id, relationship="uses", delta=2.0)

        category = self._extract_category(action_id)
        self._set_category_cooldown(category)

        self._adjust_threshold(_THRESHOLD_REJECT_SHIFT)
        logger.info("Rejected: %s (threshold->%.2f)", action_id, self._min_confidence)
        
        prefs = self._get_prefs()
        if prefs:
            prefs.record_reject()

    def _adjust_threshold(self, delta: float) -> None:
        now = time.time()
        # Stability clamp: prevent shifting threshold more often than once per hour
        if now - getattr(self, '_last_threshold_shift', 0.0) < 3600.0:
            return
            
        self._min_confidence = max(
            _THRESHOLD_FLOOR,
            min(_THRESHOLD_CEILING, self._min_confidence + delta),
        )
        self._last_threshold_shift = now

    def clear_rejections(self) -> None:
        with self._lock:
            self._rejected.clear()

    @property
    def effective_threshold(self) -> float:
        return self._min_confidence

    @property
    def feedback_stats(self) -> dict[str, Any]:
        return {
            "accepted": self._accept_count,
            "rejected": self._reject_count,
            "threshold": round(self._min_confidence, 3),
        }

    @property
    def context_snapshot(self) -> dict[str, Any]:
        """Expose current environmental context for debugging."""
        return self._get_context_snapshot()

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _humanize_action(action_id: str) -> str:
        if action_id.startswith("app:"):
            return f"Open {action_id[4:].title()}"
        if action_id.startswith("action:"):
            return action_id[7:].capitalize()
        return action_id

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


# -- Singleton ----------------------------------------------------------------
predictor = PredictiveEngine()
