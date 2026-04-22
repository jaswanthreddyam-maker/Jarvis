"""Anomaly Detector — identifies repeated failure patterns and triggers safe mode.

Monitors error classifications and performance metrics. If error frequencies
exceed safe bounds, dynamically adjusts system behavior via Safe Mode.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

from assistant.event_bus import bus, Events
from assistant.session_store import store

logger = logging.getLogger("Jarvis.AnomalyDetector")

class AnomalyDetector:
    def __init__(self) -> None:
        self._error_history = deque(maxlen=20)
        self._lock = threading.Lock()
        
        bus.subscribe(Events.ERROR_OCCURRED, self._on_error)
        bus.subscribe("system.recovery_requested", self._on_recovery)
        
        # Check initial state from session store
        crashes = store.get_crash_count()
        logger.info("Startup check: SessionStore crash_count = %d", crashes)
        if crashes >= 3:
            logger.warning("Detected %d previous crashes from SessionStore! Engaging SAFE MODE.", crashes)
            self._trigger_safe_mode("Repeated crashes on boot.")

    def _on_error(self, message: str) -> None:
        now = time.time()
        with self._lock:
            self._error_history.append(now)
            
            # Analyze patterns: 5 errors in last 60 seconds
            recent_errors = [t for t in self._error_history if now - t < 60.0]
            if len(recent_errors) >= 5:
                logger.critical("ANOMALY DETECTED: Rapid error cluster (5+ errors in 60s).")
                self._trigger_safe_mode("System instability cluster detected.")
                self._error_history.clear()

    def _on_recovery(self, restart: bool = True, target: str = "all") -> None:
        if target == "all":
            store.record_crash()
            
    def _trigger_safe_mode(self, reason: str) -> None:
        """Publishes event to drop non-critical workers and throttle operations."""
        logger.warning("Triggering SAFE MODE: %s", reason)
        bus.publish_async("system.safe_mode_engaged", reason=reason)
        # Force a UI notification
        bus.publish_async("ui.notify", title="Safe Mode Engaged", message=reason)

detector = AnomalyDetector()
