from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

try:  # pragma: no cover - imported only when FastAPI runtime is available
    from fastapi import WebSocket
except ImportError:  # pragma: no cover
    WebSocket = Any  # type: ignore[assignment]

from jarvis.application.streaming import chunk_response_text


logger = logging.getLogger("Jarvis.API.Stream")


@dataclass(slots=True)
class EventSubscription:
    event_types: set[str] = field(default_factory=set)
    request_ids: set[str] = field(default_factory=set)

    def matches(self, payload: dict[str, Any]) -> bool:
        event_type = str(payload.get("type", "") or "").strip().lower()
        request_id = str(payload.get("request_id", "") or "").strip()
        if self.event_types and event_type not in self.event_types:
            return False
        if request_id and self.request_ids and "*" not in self.request_ids and request_id not in self.request_ids:
            return False
        return True


class WebSocketEventHub:
    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._connections: dict[WebSocket, EventSubscription] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[websocket] = EventSubscription()

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.pop(websocket, None)

    async def update_subscription(self, websocket: WebSocket, payload: dict[str, Any]) -> dict[str, Any]:
        event_types = {
            str(item).strip().lower()
            for item in list(payload.get("events") or payload.get("types") or [])
            if str(item).strip()
        }
        request_ids = {
            str(item).strip()
            for item in list(payload.get("request_ids") or ["*"])
            if str(item).strip()
        }
        async with self._lock:
            if websocket in self._connections:
                self._connections[websocket] = EventSubscription(
                    event_types=event_types,
                    request_ids=request_ids,
                )
        return {
            "type": "subscribed",
            "events": sorted(event_types),
            "request_ids": sorted(request_ids),
        }

    async def send(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        await websocket.send_json(payload)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        async with self._lock:
            targets = list(self._connections.items())

        for websocket, subscription in targets:
            if not subscription.matches(payload):
                continue
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)

        for websocket in stale:
            await self.disconnect(websocket)

    def publish(self, payload: dict[str, Any]) -> None:
        message = dict(payload)

        def _schedule() -> None:
            asyncio.create_task(self.broadcast(message))

        self._loop.call_soon_threadsafe(_schedule)
