from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4


class CancelledError(RuntimeError):
    pass


class CancellationToken:
    def __init__(self, request_id: str = "") -> None:
        self.request_id = request_id
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    def cancel(self) -> bool:
        if self._cancelled.is_set():
            return False
        self._cancelled.set()
        with self._lock:
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                continue
        return True

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def add_callback(self, callback: Callable[[], None]) -> None:
        invoke_now = False
        with self._lock:
            if self._cancelled.is_set():
                invoke_now = True
            else:
                self._callbacks.append(callback)
        if invoke_now:
            callback()

    def raise_if_cancelled(self, message: str = "Request cancelled.") -> None:
        if self.is_cancelled():
            raise CancelledError(message)

    def track_subprocess(self, process: Any) -> None:
        def _stop() -> None:
            try:
                if process.poll() is not None:
                    return
                process.terminate()
                deadline = time.monotonic() + 0.5
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if process.poll() is None:
                    process.kill()
            except Exception:
                return

        self.add_callback(_stop)


class CancellationController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, CancellationToken] = {}
        self._active_request_id = ""

    def begin(self, request_id: int | str | None = None) -> CancellationToken:
        resolved = self._normalize_request_id(request_id)
        token = CancellationToken(resolved)
        with self._lock:
            self._tokens[resolved] = token
            self._active_request_id = resolved
        return token

    def finish(self, request_id: int | str | None = None) -> None:
        resolved = self._resolve_request_id(request_id)
        if not resolved:
            return
        with self._lock:
            self._tokens.pop(resolved, None)
            if self._active_request_id == resolved:
                self._active_request_id = ""

    def cancel(self, request_id: int | str | None = None) -> bool:
        resolved = self._resolve_request_id(request_id)
        if not resolved:
            return False
        with self._lock:
            token = self._tokens.get(resolved)
        if token is None:
            return False
        return token.cancel()

    def cancel_all(self) -> tuple[str, ...]:
        with self._lock:
            items = tuple(self._tokens.items())
        cancelled: list[str] = []
        for request_id, token in items:
            if token.cancel():
                cancelled.append(request_id)
        return tuple(cancelled)

    def token_for(self, request_id: int | str | None = None) -> CancellationToken | None:
        resolved = self._resolve_request_id(request_id)
        if not resolved:
            return None
        with self._lock:
            return self._tokens.get(resolved)

    @property
    def active_request_id(self) -> str:
        with self._lock:
            return self._active_request_id

    @staticmethod
    def _normalize_request_id(request_id: int | str | None) -> str:
        candidate = "" if request_id is None else str(request_id).strip()
        return candidate or uuid4().hex

    def _resolve_request_id(self, request_id: int | str | None) -> str:
        normalized = "" if request_id is None else str(request_id).strip()
        if normalized:
            return normalized
        with self._lock:
            return self._active_request_id
