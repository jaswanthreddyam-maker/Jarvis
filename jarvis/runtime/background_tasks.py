from __future__ import annotations

import asyncio

from jarvis.application.controller import JarvisApplication


class NotificationPump:
    """Poll application notifications and forward them to the runtime output queue."""

    def __init__(self, application: JarvisApplication, *, poll_interval: float = 0.25) -> None:
        self._application = application
        self._poll_interval = poll_interval

    async def run(
        self,
        output_queue: asyncio.Queue[tuple[str, str] | None],
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            notifications = await asyncio.to_thread(self._application.poll_notifications)
            for message in notifications:
                await output_queue.put(("notification", message))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                continue
