"""Intelligence Preferences - user-configurable layer for suggestions.

Controls:
    - Which suggestion categories are enabled/disabled
    - Personality settings (tone + frequency)
    - Do Not Disturb mode
    - Adaptive personality tracking (auto-shifts based on feedback)
    - Persisted as JSON so settings survive restarts
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("Jarvis.IntelPrefs")

_DEFAULT_PREFS: dict[str, Any] = {
    # -- Category toggles -----------------------------------------------------
    "categories": {
        "_apps": True,
        "_actions": True,
        "_explore": True,
        "_cold_start": True,
    },

    # -- Personality layer ----------------------------------------------------
    "personality": {
        "tone": "casual",           # "formal", "casual", "minimal"
        "frequency": "balanced",    # "eager", "balanced", "reserved"
    },

    # -- Do Not Disturb -------------------------------------------------------
    "dnd": {
        "enabled": False,
        "until": 0,                 # Unix timestamp; 0 = indefinite when enabled
    },

    # -- Adaptive personality tracking ----------------------------------------
    "adaptive": {
        "accept_streak": 0,         # Consecutive accepts
        "reject_streak": 0,         # Consecutive rejects
        "total_accepts": 0,
        "total_rejects": 0,
        "last_shift_time": 0,       # Prevent rapid shifts
    },
}

# Frequency presets
_FREQUENCY_PROFILES: dict[str, dict[str, float]] = {
    "eager": {
        "threshold_offset": -0.10,
        "cooldown_multiplier": 0.5,
        "explore_probability": 0.20,
        "suggest_threshold_offset": -0.10,
    },
    "balanced": {
        "threshold_offset": 0.0,
        "cooldown_multiplier": 1.0,
        "explore_probability": 0.10,
        "suggest_threshold_offset": 0.0,
    },
    "reserved": {
        "threshold_offset": +0.10,
        "cooldown_multiplier": 2.0,
        "explore_probability": 0.03,
        "suggest_threshold_offset": +0.10,
    },
}

# Ordered frequency levels for adaptive shifting
_FREQUENCY_ORDER = ["reserved", "balanced", "eager"]

# Tone templates
_TONE_TEMPLATES: dict[str, dict[str, str]] = {
    "formal": {
        "time":       "Based on your routine, would you like me to {action}?",
        "frequency":  "You frequently use {app} at this hour. Shall I open it?",
        "sequence":   "Following your previous action, shall I proceed to {action}?",
        "explore":    "I noticed a developing pattern. Would you like to try: {action}?",
        "cold_start": "Welcome! May I suggest: {action}?",
        "default":    "Would you like: {action}?",
    },
    "casual": {
        "time":       "Hey! {action} -- you usually do this around now.",
        "frequency":  "{app} time? You use it a lot at this hour.",
        "sequence":   "Next up: {action}?",
        "explore":    "Just a thought -- {action}?",
        "cold_start": "Hey! Try this: {action}",
        "default":    "{action}?",
    },
    "minimal": {
        "time":       "{action}?",
        "frequency":  "{action}?",
        "sequence":   "{action}?",
        "explore":    "{action}?",
        "cold_start": "{action}?",
        "default":    "{action}?",
    },
}

# Stability clamp: minimum seconds between adaptive personality shifts
_ADAPTIVE_SHIFT_COOLDOWN_SEC = 3600  # 1 hour between auto-shifts
_ADAPTIVE_STREAK_THRESHOLD = 5       # Consecutive accepts/rejects to trigger shift


class IntelligencePrefs:
    """Thread-safe, persistent preference store for intelligence modules."""

    def __init__(self, storage_dir: Path | str | None = None) -> None:
        if storage_dir is None:
            storage_dir = Path(__file__).resolve().parents[1] / "memory"
        self._storage_path = Path(storage_dir) / "intel_prefs.json"
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._load()

    # -- Persistence ----------------------------------------------------------

    def _load(self) -> None:
        if self._storage_path.exists():
            try:
                with open(self._storage_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info("Loaded intelligence preferences")
            except Exception:
                self._data = {}
        # Merge defaults for any missing keys
        for key, default in _DEFAULT_PREFS.items():
            if key not in self._data:
                self._data[key] = (
                    dict(default) if isinstance(default, dict) else default
                )
            elif isinstance(default, dict):
                for k, v in default.items():
                    self._data[key].setdefault(k, v)

    def _save(self) -> None:
        try:
            with open(self._storage_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save preferences: %s", e)

    # -- Do Not Disturb -------------------------------------------------------

    @property
    def dnd_active(self) -> bool:
        """Check if DND is currently active."""
        with self._lock:
            dnd = self._data.get("dnd", {})
            if not dnd.get("enabled", False):
                return False
            until = dnd.get("until", 0)
            # Indefinite DND (until=0) or timed DND still active
            if until == 0:
                return True
            if time.time() < until:
                return True
            # Timer expired -- auto-disable
            dnd["enabled"] = False
            dnd["until"] = 0
        self._save()
        return False

    def enable_dnd(self, duration_minutes: int = 0) -> None:
        """Enable Do Not Disturb. duration=0 means indefinite."""
        with self._lock:
            self._data.setdefault("dnd", {})
            self._data["dnd"]["enabled"] = True
            if duration_minutes > 0:
                self._data["dnd"]["until"] = time.time() + duration_minutes * 60
            else:
                self._data["dnd"]["until"] = 0
        self._save()
        if duration_minutes:
            logger.info("DND enabled for %d minutes", duration_minutes)
        else:
            logger.info("DND enabled (indefinite)")

    def disable_dnd(self) -> None:
        with self._lock:
            self._data.setdefault("dnd", {})
            self._data["dnd"]["enabled"] = False
            self._data["dnd"]["until"] = 0
        self._save()
        logger.info("DND disabled")

    # -- Category management --------------------------------------------------

    def is_category_enabled(self, category: str) -> bool:
        """Priority: DND > specific override > broad class > default True."""
        if self.dnd_active:
            return False
        with self._lock:
            cats = self._data.get("categories", {})
            if category in cats:
                return bool(cats[category])
            return bool(cats.get("_apps", True))

    def is_source_enabled(self, source: str) -> bool:
        if self.dnd_active:
            return False
        with self._lock:
            cats = self._data.get("categories", {})
            if source == "explore":
                return bool(cats.get("_explore", True))
            if source == "cold_start":
                return bool(cats.get("_cold_start", True))
            return True

    def set_category_enabled(self, category: str, enabled: bool) -> None:
        with self._lock:
            self._data.setdefault("categories", {})
            self._data["categories"][category] = enabled
        self._save()
        logger.info("Category '%s' %s", category, "enabled" if enabled else "disabled")

    def get_disabled_categories(self) -> list[str]:
        with self._lock:
            cats = self._data.get("categories", {})
            return [k for k, v in cats.items() if not v]

    def get_all_categories(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._data.get("categories", {}))

    # -- Personality ----------------------------------------------------------

    @property
    def tone(self) -> str:
        with self._lock:
            return self._data.get("personality", {}).get("tone", "casual")

    @tone.setter
    def tone(self, value: str) -> None:
        if value not in _TONE_TEMPLATES:
            logger.warning("Unknown tone '%s', ignoring", value)
            return
        with self._lock:
            self._data.setdefault("personality", {})
            self._data["personality"]["tone"] = value
        self._save()
        logger.info("Personality tone set to '%s'", value)

    @property
    def frequency(self) -> str:
        with self._lock:
            return self._data.get("personality", {}).get("frequency", "balanced")

    @frequency.setter
    def frequency(self, value: str) -> None:
        if value not in _FREQUENCY_PROFILES:
            logger.warning("Unknown frequency '%s', ignoring", value)
            return
        with self._lock:
            self._data.setdefault("personality", {})
            self._data["personality"]["frequency"] = value
        self._save()
        logger.info("Personality frequency set to '%s'", value)

    @property
    def frequency_profile(self) -> dict[str, float]:
        return _FREQUENCY_PROFILES.get(self.frequency, _FREQUENCY_PROFILES["balanced"])

    # -- Tone formatting ------------------------------------------------------

    def format_suggestion(self, label: str, source: str,
                           category: str = "") -> str:
        templates = _TONE_TEMPLATES.get(self.tone, _TONE_TEMPLATES["casual"])
        template = templates.get(source, templates["default"])
        app_name = category.title() if category else label
        return template.format(action=label, app=app_name)

    # -- Adaptive personality (feedback-driven) --------------------------------

    def record_accept(self) -> str | None:
        """Record an acceptance. Returns new frequency level if shifted."""
        with self._lock:
            ad = self._data.setdefault("adaptive", {})
            ad["accept_streak"] = ad.get("accept_streak", 0) + 1
            ad["reject_streak"] = 0  # Reset opposite streak
            ad["total_accepts"] = ad.get("total_accepts", 0) + 1
        self._save()
        return self._maybe_shift_frequency("up")

    def record_reject(self) -> str | None:
        """Record a rejection. Returns new frequency level if shifted."""
        with self._lock:
            ad = self._data.setdefault("adaptive", {})
            ad["reject_streak"] = ad.get("reject_streak", 0) + 1
            ad["accept_streak"] = 0
            ad["total_rejects"] = ad.get("total_rejects", 0) + 1
        self._save()
        return self._maybe_shift_frequency("down")

    def _maybe_shift_frequency(self, direction: str) -> str | None:
        """Shift frequency level if streak threshold is met
        AND enough time has passed since the last shift (stability clamp)."""
        with self._lock:
            ad = self._data.get("adaptive", {})
            now = time.time()

            # Stability clamp: prevent rapid shifts
            last_shift = ad.get("last_shift_time", 0)
            if now - last_shift < _ADAPTIVE_SHIFT_COOLDOWN_SEC:
                return None

            streak_key = "accept_streak" if direction == "up" else "reject_streak"
            streak = ad.get(streak_key, 0)
            if streak < _ADAPTIVE_STREAK_THRESHOLD:
                return None

            current = self._data.get("personality", {}).get("frequency", "balanced")
            idx = _FREQUENCY_ORDER.index(current) if current in _FREQUENCY_ORDER else 1

            if direction == "up" and idx < len(_FREQUENCY_ORDER) - 1:
                new_freq = _FREQUENCY_ORDER[idx + 1]
            elif direction == "down" and idx > 0:
                new_freq = _FREQUENCY_ORDER[idx - 1]
            else:
                return None  # Already at boundary

            # Apply shift
            self._data.setdefault("personality", {})
            self._data["personality"]["frequency"] = new_freq
            ad["last_shift_time"] = now
            ad[streak_key] = 0  # Reset streak after shift

        self._save()
        logger.info("Adaptive personality shifted frequency to '%s' "
                     "(direction=%s, streak=%d)", new_freq, direction, streak)
        return new_freq

    @property
    def adaptive_stats(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get("adaptive", {}))

    # -- Snapshot -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


# -- Singleton ----------------------------------------------------------------
intel_prefs = IntelligencePrefs()
