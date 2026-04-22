from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("Jarvis.EventBus")


class Events:
    """Standardized event channel names."""
    STATE_CHANGED = "state.status_changed"
    WAKE_DETECTED = "wake.detected"
    TTS_STARTED = "tts.started"
    ERROR_OCCURRED = "system.error_occurred"
    PERF_MODE_CHANGED = "state.perf_mode_changed"
    METRICS_UPDATED = "metrics.updated"
    CONFIG_RELOADED = "system.config_reloaded"
    # Memory Graph
    MEMORY_UPDATED = "memory.graph_updated"
    # Voice Identity
    VOICE_VERIFIED = "voice.identity_verified"
    VOICE_ENROLLED = "voice.identity_enrolled"
    VOICE_GUEST = "voice.guest_detected"
    # Predictive + Proactive
    PREDICTION_READY = "proactive.prediction_ready"
    SUGGESTION_READY = "proactive.suggestion"
    AUTO_ACT_TRIGGERED = "proactive.auto_act"


class EventBus:
    """
    Standard event channels:
        execution.step_start   — fired before a step runs
        execution.step_done    — fired after a step succeeds
        execution.step_failed  — fired after a step fails
        execution.step_skipped — fired when a step is skipped (interrupt/validation)
        execution.feedback     — general execution feedback for UI
        execution.interrupted  — fired when the whole execution is interrupted
        action.started         — raw action dispatch started
        action.completed       — raw action completed
        action.failed          — raw action failed
        permission.blocked     — action denied by permission policy
        permission.confirm     — action requires user confirmation
        reminder.due           — scheduled reminder is due
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable]] = {}
        import concurrent.futures
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="EventBusAsync")
        import atexit
        atexit.register(self._executor.shutdown, wait=False)

    def subscribe(self, event_type: str, callback: Callable) -> None:
        """Register a callback for a specific event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable) -> None:
        """Remove a previously registered callback."""
        listeners = self._subscribers.get(event_type, [])
        if callback in listeners:
            listeners.remove(callback)

    def publish(self, event_type: str, *args: Any, **kwargs: Any) -> None:
        """Dispatch an event to all registered subscribers synchronously."""
        for callback in self._subscribers.get(event_type, []):
            try:
                callback(*args, **kwargs)
            except Exception as exc:
                logger.warning("Event handler for '%s' raised: %s", event_type, exc)

    def publish_async(self, event_type: str, *args: Any, **kwargs: Any) -> None:
        """Dispatch an event asynchronously using the thread pool."""
        def _dispatch():
            for callback in self._subscribers.get(event_type, []):
                try:
                    callback(*args, **kwargs)
                except Exception as exc:
                    logger.warning("Async event handler for '%s' raised: %s", event_type, exc)
        self._executor.submit(_dispatch)

    def shutdown(self):
        """Clean up the thread pool executor."""
        self._executor.shutdown(wait=False)

    def has_subscribers(self, event_type: str) -> bool:
        """Check whether any listener is registered for the event type."""
        return bool(self._subscribers.get(event_type))


bus = EventBus()
