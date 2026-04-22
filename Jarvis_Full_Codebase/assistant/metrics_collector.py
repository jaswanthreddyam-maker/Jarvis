"""Metrics Collector — aggregates system performance and interaction latency.

Tracks CPU, memory, wake detection timing, and API latency.
Publishes aggregated stats via METRICS_UPDATED event.
"""
from __future__ import annotations

import logging
import threading
import time

try:
    import psutil
except ImportError:
    psutil = None

from assistant.event_bus import bus, Events

logger = logging.getLogger("Jarvis.MetricsCollector")

class MetricsCollector(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="MetricsCollector")
        self._running = True
        self._latencies: list[float] = []
        self._wake_timings: list[float] = []
        self._lock = threading.Lock()
        
        bus.subscribe("metrics.record_latency", self._record_latency)
        bus.subscribe("metrics.record_wake_timing", self._record_wake_timing)

    def _record_latency(self, latency_ms: float) -> None:
        with self._lock:
            self._latencies.append(latency_ms)
            if len(self._latencies) > 50:
                self._latencies.pop(0)

    def _record_wake_timing(self, duration_ms: float) -> None:
        with self._lock:
            self._wake_timings.append(duration_ms)
            if len(self._wake_timings) > 50:
                self._wake_timings.pop(0)

    def run(self) -> None:
        if not psutil:
            logger.warning("psutil not available, skipping resource metrics.")
            return

        logger.info("Metrics Collector started.")
        while self._running:
            try:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory().percent
                
                with self._lock:
                    avg_latency = sum(self._latencies)/len(self._latencies) if self._latencies else 0.0
                    avg_wake = sum(self._wake_timings)/len(self._wake_timings) if self._wake_timings else 0.0
                
                metrics = {
                    "cpu": cpu,
                    "ram": mem,
                    "avg_latency_ms": avg_latency,
                    "avg_wake_timing_ms": avg_wake,
                }
                
                bus.publish_async(Events.METRICS_UPDATED, metrics)
            except Exception as e:
                logger.error("Failed to collect metrics: %s", e)
                
            time.sleep(2.0)

    def stop(self) -> None:
        self._running = False
