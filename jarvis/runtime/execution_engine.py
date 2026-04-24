from __future__ import annotations

import asyncio
import logging

from jarvis.application.controller import JarvisApplication


class ExecutionEngine:
    def __init__(
        self,
        application: JarvisApplication,
        *,
        timeout_seconds: float = 30.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._application = application
        self._timeout_seconds = timeout_seconds
        self._logger = logger or logging.getLogger("Jarvis.RuntimeExecution")

    async def execute(
        self,
        decision: Any,
        *,
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> str:
        timeout = timeout_seconds if timeout_seconds is not None else self._timeout_seconds
        self._application.begin_execution(request_id)
        self._logger.info(
            "Runtime request started.",
            extra={
                "event": "runtime.request.started",
                "request_id": request_id,
                "timeout_seconds": timeout,
            },
        )
        try:
            response, _ = await asyncio.wait_for(
                self._application.handle_text_async(decision, request_id=request_id),
                timeout=timeout,
            )
            self._logger.info(
                "Runtime request completed.",
                extra={
                    "event": "runtime.request.completed",
                    "request_id": request_id,
                },
            )
            return response
        except asyncio.CancelledError:
            self._application.cancel_active(request_id)
            raise
        except asyncio.TimeoutError:
            self._logger.warning("Request %s timed out after %.2fs", request_id, timeout)
            self._application.cancel_active(request_id)
            return "That request timed out, so I cancelled it to keep Jarvis responsive."
        finally:
            self._application.finish_execution(request_id)
