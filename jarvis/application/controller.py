from __future__ import annotations

import asyncio

from jarvis.config.settings import Settings
from jarvis.monitoring import JarvisHealthService


class JarvisApplication:
    def __init__(
        self,
        orchestrator,
        settings: Settings | None = None,
        health_service: JarvisHealthService | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._settings = settings
        self._health_service = health_service

    @property
    def orchestrator(self):
        return self._orchestrator

    @property
    def settings(self) -> Settings | None:
        return self._settings

    def subscribe_runtime_event(self, event_name: str, callback) -> None:
        event_bus = getattr(self.orchestrator, "_event_bus", None)
        if event_bus is not None:
            event_bus.subscribe(event_name, callback)

    def unsubscribe_runtime_event(self, event_name: str, callback) -> None:
        event_bus = getattr(self.orchestrator, "_event_bus", None)
        if event_bus is not None:
            event_bus.unsubscribe(event_name, callback)

    def begin_execution(self, request_id: int | str | None = None) -> None:
        cancellation_controller = getattr(self.orchestrator, "_cancellation_controller", None)
        if cancellation_controller is not None:
            cancellation_controller.begin(request_id)

    def cancel_active(self, request_id: int | str | None = None) -> bool:
        cancellation_controller = getattr(self.orchestrator, "_cancellation_controller", None)
        cancelled = False
        if cancellation_controller is not None:
            cancelled = cancellation_controller.cancel(request_id)
        self.orchestrator.interrupt(request_id=request_id)
        return cancelled

    def finish_execution(self, request_id: int | str | None = None) -> None:
        self.orchestrator.finish_request(request_id)

    def handle_text(
        self,
        user_input: Any,
        request_id: int | str | None = None,
    ) -> tuple[str, dict[str, object] | None]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(self.handle_text_async(user_input, request_id=request_id), loop).result()
        return asyncio.run(self.handle_text_async(user_input, request_id=request_id))

    async def handle_text_async(
        self,
        user_input: Any,
        request_id: int | str | None = None,
    ) -> tuple[str, dict[str, object] | None]:
        return await self.orchestrator.handle_text_async(user_input, request_id=request_id)

    def poll_notifications(self) -> list[str]:
        return self.orchestrator.poll_notifications()

    def health_snapshot(self) -> dict[str, object]:
        if self._health_service is None:
            return {"status": "unknown"}
        return self._health_service.snapshot(application=self)

    async def health_snapshot_async(self) -> dict[str, object]:
        if self._health_service is None:
            return {"status": "unknown"}
        return await self._health_service.snapshot_async(application=self)

    def shutdown(self) -> None:
        if hasattr(self.orchestrator, "shutdown"):
            self.orchestrator.shutdown()
