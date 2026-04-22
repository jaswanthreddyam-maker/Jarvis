"""Global Application State Manager.

Acts as the central source of truth for the application state,
decoupling the backend from UI-specific state frameworks (like Qt).
Uses the EventBus to broadcast state changes.
"""
from __future__ import annotations

import threading
from typing import Any

from assistant.event_bus import EventBus


class GlobalAppState:
    """Thread-safe global state manager."""
    _instance: GlobalAppState | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, event_bus: EventBus | None = None) -> GlobalAppState:
        with cls._lock:
            if cls._instance is None:
                if event_bus is None:
                    from assistant.event_bus import bus
                    event_bus = bus
                cls._instance = cls(event_bus)
        return cls._instance

    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        self._status = "IDLE"
        self._perf_mode = "HIGH"
        self._wake_enabled = True
        self._backend_online = False
        
        self._state_lock = threading.Lock()

    @property
    def status(self) -> str:
        with self._state_lock:
            return self._status

    def set_status(self, new_status: str) -> None:
        """Update the central status (IDLE, LISTENING, THINKING, SPEAKING)."""
        with self._state_lock:
            if self._status == new_status:
                return
            self._status = new_status
        self._bus.publish("state.status_changed", new_status)

    @property
    def perf_mode(self) -> str:
        with self._state_lock:
            return self._perf_mode

    def set_perf_mode(self, mode: str) -> None:
        """Update performance mode (HIGH or LOW)."""
        with self._state_lock:
            if self._perf_mode == mode:
                return
            self._perf_mode = mode
        self._bus.publish("state.perf_mode_changed", mode)

    @property
    def wake_enabled(self) -> bool:
        with self._state_lock:
            return self._wake_enabled

    def set_wake_enabled(self, enabled: bool) -> None:
        """Toggle the wake word listener globally."""
        with self._state_lock:
            if self._wake_enabled == enabled:
                return
            self._wake_enabled = enabled
        self._bus.publish("state.wake_enabled_changed", enabled)

    @property
    def backend_online(self) -> bool:
        with self._state_lock:
            return self._backend_online

    def set_backend_online(self, online: bool) -> None:
        with self._state_lock:
            if self._backend_online == online:
                return
            self._backend_online = online
        self._bus.publish("state.backend_online_changed", online)


# Global singleton instance
global_state = GlobalAppState.get_instance()
