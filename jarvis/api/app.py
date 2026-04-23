from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from inspect import isawaitable
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

try:  # pragma: no cover - depends on local environment
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
except ImportError:  # pragma: no cover
    FastAPI = None  # type: ignore[assignment]
    WebSocket = Any  # type: ignore[assignment]
    WebSocketDisconnect = RuntimeError  # type: ignore[assignment]

    class HTTPException(RuntimeError):
        def __init__(self, status_code: int, detail: Any = None) -> None:
            super().__init__(str(detail))
            self.status_code = status_code
            self.detail = detail

from jarvis.api.streaming import WebSocketEventHub
from jarvis.application.bootstrap import build_application
from jarvis.config.settings import Settings, load_settings
from jarvis.observability import configure_logging


logger = logging.getLogger("Jarvis.API")


class ExecuteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    request_id: str | None = Field(default=None, max_length=128)


class ExecuteResponse(BaseModel):
    request_id: str
    response: str
    task_snapshot: dict[str, object] | None = None


async def _broadcast_request_complete(app: FastAPI, *, request_id: str, response: str, task_snapshot: dict[str, object] | None) -> None:
    hub = getattr(app.state, "websocket_hub", None)
    if hub is None:
        return
    await hub.broadcast(
        {
            "type": "request_complete",
            "request_id": request_id,
            "response": response,
            "task_snapshot": task_snapshot,
        }
    )


async def _broadcast_request_failed(app: FastAPI, *, request_id: str, message: str) -> None:
    hub = getattr(app.state, "websocket_hub", None)
    if hub is None:
        return
    await hub.broadcast(
        {
            "type": "request_failed",
            "request_id": request_id,
            "error": message,
        }
    )


def create_app(
    settings: Settings | None = None,
    *,
    application_factory=None,
):
    if FastAPI is None:
        raise RuntimeError("fastapi is not installed in this environment.")
    resolved_settings = settings or load_settings()
    configure_logging(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            application = application_factory() if application_factory is not None else build_application(settings=resolved_settings)
            if isawaitable(application):
                application = await application
        except Exception:
            logger.exception("Jarvis API startup failed.", extra={"event": "api.startup.failed"})
            raise
        websocket_hub = WebSocketEventHub()
        app.state.application = application
        app.state.settings = resolved_settings
        app.state.websocket_hub = websocket_hub
        for event_name in ("runtime.status", "runtime.response_chunk", "runtime.execution_update"):
            if hasattr(application, "subscribe_runtime_event"):
                application.subscribe_runtime_event(event_name, websocket_hub.publish)
        logger.info(
            "Jarvis API started.",
            extra={
                "event": "api.started",
                "host": resolved_settings.api.host,
                "port": resolved_settings.api.port,
            },
        )
        try:
            yield
        finally:
            if hasattr(application, "shutdown"):
                application.shutdown()
            logger.info("Jarvis API stopped.", extra={"event": "api.stopped"})

    app = FastAPI(
        title="Jarvis API",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return await app.state.application.health_snapshot_async()

    @app.get("/ready")
    async def ready() -> dict[str, object]:
        snapshot = await app.state.application.health_snapshot_async()
        status_code = snapshot.get("status")
        if status_code not in {"ok", "configured", "unknown"}:
            raise HTTPException(status_code=503, detail=snapshot)
        return snapshot

    @app.post("/v1/execute", response_model=ExecuteResponse)
    async def execute(payload: ExecuteRequest) -> ExecuteResponse:
        request_id = payload.request_id or uuid4().hex
        try:
            response, task_snapshot = await app.state.application.handle_text_async(
                payload.text,
                request_id=request_id,
            )
            await _broadcast_request_complete(
                app,
                request_id=request_id,
                response=response,
                task_snapshot=task_snapshot,
            )
        except Exception as exc:
            logger.exception(
                "API execution failed.",
                extra={"event": "api.execution.failed", "request_id": request_id},
            )
            await _broadcast_request_failed(
                app,
                request_id=request_id,
                message="Execution failed, but the service is still running.",
            )
            raise HTTPException(
                status_code=500,
                detail="Execution failed, but the service is still running.",
            ) from exc

        return ExecuteResponse(
            request_id=request_id,
            response=response,
            task_snapshot=task_snapshot,
        )

    @app.post("/v1/cancel/{request_id}")
    async def cancel(request_id: str) -> dict[str, object]:
        cancelled = bool(app.state.application.cancel_active(request_id))
        return {"request_id": request_id, "cancelled": cancelled}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await app.state.websocket_hub.connect(websocket)
        await app.state.websocket_hub.send(
            websocket,
            {
                "type": "hello",
                "backend": "Jarvis API",
                "safe_mode": bool(app.state.settings.safe_mode),
            },
        )
        try:
            while True:
                message = await websocket.receive_json()
                message_type = str(message.get("type", "") or "").strip().lower()

                if message_type == "subscribe":
                    subscribed = await app.state.websocket_hub.update_subscription(websocket, message)
                    await app.state.websocket_hub.send(websocket, subscribed)
                    continue

                if message_type == "execute":
                    text = str(message.get("text", "") or "").strip()
                    request_id = str(message.get("request_id") or uuid4().hex)
                    if not text:
                        await app.state.websocket_hub.send(
                            websocket,
                            {
                                "type": "request_failed",
                                "request_id": request_id,
                                "error": "A non-empty text request is required.",
                            },
                        )
                        continue

                    async def _run_request(*, request_text: str = text, current_request_id: str = request_id) -> None:
                        try:
                            response, task_snapshot = await app.state.application.handle_text_async(
                                request_text,
                                request_id=current_request_id,
                            )
                            await _broadcast_request_complete(
                                app,
                                request_id=current_request_id,
                                response=response,
                                task_snapshot=task_snapshot,
                            )
                        except Exception:
                            logger.exception(
                                "WebSocket execution failed.",
                                extra={"event": "api.websocket.execution.failed", "request_id": current_request_id},
                            )
                            await _broadcast_request_failed(
                                app,
                                request_id=current_request_id,
                                message="Execution failed, but the service is still running.",
                            )

                    asyncio.create_task(_run_request())
                    continue

                if message_type == "cancel":
                    request_id = str(message.get("request_id", "") or "").strip()
                    cancelled = bool(app.state.application.cancel_active(request_id))
                    await app.state.websocket_hub.send(
                        websocket,
                        {
                            "type": "cancel_ack",
                            "request_id": request_id,
                            "cancelled": cancelled,
                        },
                    )
                    continue

                await app.state.websocket_hub.send(
                    websocket,
                    {
                        "type": "error",
                        "error": f"Unsupported websocket message type '{message_type or 'unknown'}'.",
                    },
                )
        except WebSocketDisconnect:
            await app.state.websocket_hub.disconnect(websocket)

    return app
