"""Session Store — persists recent state/context across crashes.

Automatically saves critical app state to disk periodically.
On startup, restores user context, active tasks, and preferences.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from assistant.event_bus import bus, Events

logger = logging.getLogger("Jarvis.SessionStore")

class SessionStore:
    def __init__(self, storage_dir: Path | str | None = None) -> None:
        if storage_dir is None:
            storage_dir = Path(__file__).resolve().parents[1] / "memory"
        self._file_path = Path(storage_dir) / "session.json"
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._state: dict[str, Any] = self._load()
        self._lock = threading.Lock()
        self._dirty = False
        
        # Subscribe to track context
        bus.subscribe(Events.STATE_CHANGED, self._track_state)
        bus.subscribe("execution.step_start", self._track_step)
        
        self._running = True
        self._thread = threading.Thread(target=self._flush_loop, daemon=True, name="SessionStore")
        self._thread.start()

    def _load(self) -> dict[str, Any]:
        if self._file_path.exists():
            try:
                with open(self._file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info("Restored session state from %s (crash_count=%d)", 
                                self._file_path, data.get("crash_count", 0))
                    return data
            except Exception as e:
                logger.error("Failed to load session store: %s", e)
        return {"last_status": "IDLE", "last_task": None, "crash_count": 0}

    def _track_state(self, status: str) -> None:
        with self._lock:
            self._state["last_status"] = status
            self._dirty = True

    def _track_step(self, step: Any) -> None:
        with self._lock:
            self._state["last_task"] = str(step)
            self._dirty = True

    def get_last_task(self) -> str | None:
        with self._lock:
            return self._state.get("last_task")

    def record_crash(self) -> None:
        with self._lock:
            self._state["crash_count"] = self._state.get("crash_count", 0) + 1
            self._dirty = True
            self._flush()

    def get_crash_count(self) -> int:
        with self._lock:
            return self._state.get("crash_count", 0)

    def reset_crashes(self) -> None:
        with self._lock:
            self._state["crash_count"] = 0
            self._dirty = True

    def _flush(self) -> None:
        if not self._dirty:
            return
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f)
            self._dirty = False
        except Exception as e:
            logger.error("Failed to flush session store: %s", e)

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(5.0)
            with self._lock:
                self._flush()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._flush()

# Singleton instance
store = SessionStore()
