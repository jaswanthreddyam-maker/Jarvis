"""Health Monitor — watchdog system for detecting and recovering from silent failures.

Continuously monitors critical threads (wake listener, ASR, TTS).
If a thread hangs or dies, it triggers a recovery via LifecycleManager.
"""
from __future__ import annotations

import logging
import threading
import time

from assistant.event_bus import bus, Events
from enum import Enum

logger = logging.getLogger("Jarvis.HealthMonitor")

class ErrorClassification(Enum):
    WARNING = "warning"
    RECOVERABLE = "recoverable"
    CRITICAL = "critical"

class HealthMonitor(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="HealthMonitor")
        self._running = True
        self._heartbeats: dict[str, float] = {}
        self._lock = threading.Lock()
        self._retry_counts: dict[str, int] = {}
        self._last_recovery_time: dict[str, float] = {}
        
        # Subscribe to heartbeats
        bus.subscribe("health.heartbeat", self._on_heartbeat)

    def _on_heartbeat(self, component: str) -> None:
        with self._lock:
            self._heartbeats[component] = time.time()
            # Reset retry count if component is healthy for > 60s
            if component in self._last_recovery_time:
                if time.time() - self._last_recovery_time[component] > 60.0:
                    self._retry_counts[component] = 0

    def run(self) -> None:
        logger.info("Health Monitor watchdog started.")
        # Give components time to start (especially models like Whisper/Coqui)
        time.sleep(30.0)
        
        while self._running:
            now = time.time()
            failed_component = None
            
            with self._lock:
                for component, last_beat in self._heartbeats.items():
                    # If no heartbeat for 10 seconds, consider it dead
                    if now - last_beat > 10.0:
                        failed_component = component
                        break
                        
            if failed_component:
                self._handle_failure(failed_component)
                
            time.sleep(2.0)

    def _handle_failure(self, component: str) -> None:
        now = time.time()
        
        with self._lock:
            retries = self._retry_counts.get(component, 0)
            
            # Exponential backoff check
            if retries > 0:
                backoff_delay = min(120, 2 ** retries)
                last_time = self._last_recovery_time.get(component, 0)
                if now - last_time < backoff_delay:
                    logger.debug("Skipping recovery for %s (backoff: %ds)", component, backoff_delay)
                    return
            
            self._retry_counts[component] = retries + 1
            self._last_recovery_time[component] = now

        classification = ErrorClassification.RECOVERABLE
        if retries >= 3:
            classification = ErrorClassification.CRITICAL

        logger.error("WATCHDOG TRIGGERED: Component %s is unresponsive! Classification: %s", component, classification.name)
        bus.publish(Events.ERROR_OCCURRED, f"Silent failure in {component} ({classification.name})")
        
        self._recover(component, classification)

    def _recover(self, component: str, classification: ErrorClassification) -> None:
        if classification == ErrorClassification.CRITICAL:
            logger.warning("Initiating FULL auto-recovery sequence via EventBus...")
            self._running = False
            bus.publish_async("system.recovery_requested", restart=True, target="all")
        else:
            logger.warning("Initiating PARTIAL recovery for %s...", component)
            bus.publish_async("system.recovery_requested", restart=True, target=component)

    def stop(self) -> None:
        self._running = False
