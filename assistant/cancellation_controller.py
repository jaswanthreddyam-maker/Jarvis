from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
import logging

logger = logging.getLogger("Jarvis.Cancellation")


class CancelledError(RuntimeError):
    """Raised when an in-flight action is cancelled cooperatively."""


class CancellationToken:
    def __init__(self, request_id: str = "") -> None:
        self.request_id = request_id
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    def cancel(self) -> bool:
        if self._event.is_set():
            return False

        self._event.set()
        with self._lock:
            callbacks = list(self._callbacks)
            self._callbacks.clear()

        for callback in callbacks:
            try:
                callback()
            except Exception as e:
                logger.error("Cancellation callback failed: %s", e)
        return True

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def add_callback(self, callback: Callable[[], None]) -> None:
        invoke_now = False
        with self._lock:
            if self._event.is_set():
                invoke_now = True
            else:
                self._callbacks.append(callback)

        if invoke_now:
            callback()

    def raise_if_cancelled(self, message: str = "Request cancelled.") -> None:
        if self.is_cancelled():
            raise CancelledError(message)

    def track_subprocess(self, process: Any) -> None:
        def _terminate() -> None:
            try:
                if process.poll() is not None:
                    return
                process.terminate()
                deadline = time.monotonic() + 0.5
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if process.poll() is None:
                    process.kill()
            except Exception as e:
                logger.error("Failed to terminate subprocess during cancellation: %s", e)

        self.add_callback(_terminate)


class CancellationController:
    """Tracks the currently active backend request and its cancellation token."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_request_id = ""
        self._active_task_id = ""
        self._token: CancellationToken | None = None

    def begin(self, request_id: int | str | None = None) -> CancellationToken:
        token = CancellationToken("" if request_id is None else str(request_id))
        with self._lock:
            self._active_request_id = token.request_id
            self._active_task_id = ""
            self._token = token
        return token

    def set_active_task(self, task_id: str) -> None:
        with self._lock:
            if self._token is None:
                return
            self._active_task_id = task_id.strip()

    def cancel(self, request_id: int | str | None = None) -> bool:
        with self._lock:
            token = self._token
            active_request_id = self._active_request_id

        if token is None:
            return False
        if request_id is not None and str(request_id) != active_request_id:
            return False
        return token.cancel()

    def finish(self, request_id: int | str | None = None) -> None:
        with self._lock:
            if self._token is None:
                return
            if request_id is not None and str(request_id) != self._active_request_id:
                return
            self._token = None
            self._active_request_id = ""
            self._active_task_id = ""

    @property
    def current_token(self) -> CancellationToken | None:
        with self._lock:
            return self._token

    @property
    def active_task_id(self) -> str:
        with self._lock:
            return self._active_task_id
