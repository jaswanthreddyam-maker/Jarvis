"""Performance Manager — monitors system load and optimizes Jarvis.

Lowers UI rendering FPS and pauses heavy background tasks when the system is under load
or when Jarvis is idle.
"""
from __future__ import annotations

import logging
import threading
import time

try:
    import psutil
except ImportError:
    psutil = None

from assistant.global_state import GlobalAppState
from assistant.event_bus import EventBus, Events
from assistant.settings import Settings

logger = logging.getLogger("Jarvis.PerformanceManager")


class PerformanceManager(threading.Thread):
    def __init__(self, state: GlobalAppState, bus: EventBus, settings: Settings) -> None:
        super().__init__(daemon=True, name="PerformanceManager")
        self.state = state
        self.bus = bus
        self._running = True
        
        # Load config
        try:
            perf_config = settings.get_config().get("performance", {})
        except AttributeError:
            perf_config = {}
            
        self._monitor_cpu = perf_config.get("monitor_cpu", True)
        self._max_idle_cpu = float(perf_config.get("max_idle_cpu", 65.0))
        
        if not psutil:
            logger.warning("psutil not found, PerformanceManager disabled.")
            self._monitor_cpu = False

    def run(self) -> None:
        if not self._monitor_cpu:
            return

        logger.info("Performance Manager started (Max Idle CPU: %.1f%%)", self._max_idle_cpu)
        
        while self._running:
            try:
                cpu = psutil.cpu_percent(interval=1.0)
                status = self.state.status
                
                # Dynamic scaling logic
                if status == "IDLE":
                    # If idle but system is under heavy load from other apps, save resources
                    if cpu > self._max_idle_cpu:
                        if self.state.perf_mode != "LOW":
                            logger.info("System load high (%.1f%% CPU). Enabling LOW performance mode.", cpu)
                            self.state.set_perf_mode("LOW")
                            self.bus.publish(Events.PERF_MODE_CHANGED, "LOW")
                    elif cpu < (self._max_idle_cpu - 15.0):
                        if self.state.perf_mode != "HIGH":
                            logger.info("System load normal (%.1f%% CPU). Restoring HIGH performance mode.", cpu)
                            self.state.set_perf_mode("HIGH")
                            self.bus.publish(Events.PERF_MODE_CHANGED, "HIGH")
                else:
                    # When interacting, prioritize responsiveness
                    if self.state.perf_mode != "HIGH":
                        self.state.set_perf_mode("HIGH")
                        self.bus.publish(Events.PERF_MODE_CHANGED, "HIGH")
                
                self.bus.publish(Events.METRICS_UPDATED, {"cpu": cpu})
                
            except Exception as e:
                logger.error("Performance check failed: %s", e)
                
            time.sleep(1.0)

    def stop(self) -> None:
        self._running = False
