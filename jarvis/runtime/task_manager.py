from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


class RuntimeTaskManager:
    """Track active runtime request tasks and coordinate cancellation."""

    def __init__(self, *, max_concurrent_requests: int = 2) -> None:
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._latest_request_id = ""
        self._max_concurrent_requests = max(1, int(max_concurrent_requests))
        self._slots = asyncio.Semaphore(self._max_concurrent_requests)

    @property
    def latest_request_id(self) -> str:
        return self._latest_request_id

    def register(self, request_id: str, task: asyncio.Task[Any]) -> None:
        self._active_tasks[request_id] = task
        self._latest_request_id = request_id

    def complete(self, request_id: str) -> None:
        removed = self._active_tasks.pop(request_id, None)
        if self._latest_request_id == request_id:
            remaining = tuple(self._active_tasks)
            self._latest_request_id = remaining[-1] if remaining else ""
        if removed is not None:
            self._slots.release()

    def active_request_ids(self) -> tuple[str, ...]:
        return tuple(self._active_tasks)

    @property
    def max_concurrent_requests(self) -> int:
        return self._max_concurrent_requests

    async def acquire_slot(self) -> None:
        await self._slots.acquire()

    def release_slot(self) -> None:
        self._slots.release()

    def cancel(
        self,
        *,
        request_id: str | None = None,
        cancel_callback: Callable[[str], Any] | None = None,
    ) -> tuple[str, ...]:
        target_ids = self._resolve_target_ids(request_id)
        for target_id in target_ids:
            if cancel_callback is not None:
                cancel_callback(target_id)
            task = self._active_tasks.get(target_id)
            if task is not None and not task.done():
                task.cancel()
        return target_ids

    def cancel_all(
        self,
        *,
        cancel_callback: Callable[[str], Any] | None = None,
    ) -> tuple[str, ...]:
        target_ids = self.active_request_ids()
        for target_id in target_ids:
            if cancel_callback is not None:
                cancel_callback(target_id)
            task = self._active_tasks.get(target_id)
            if task is not None and not task.done():
                task.cancel()
        return target_ids

    async def wait_for_idle(self) -> None:
        tasks = tuple(self._active_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _resolve_target_ids(self, request_id: str | None) -> tuple[str, ...]:
        normalized = (request_id or "").strip()
        if normalized:
            return (normalized,) if normalized in self._active_tasks else tuple()
        if self._latest_request_id and self._latest_request_id in self._active_tasks:
            return (self._latest_request_id,)
        return tuple()
