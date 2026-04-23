from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from jarvis.config.settings import Settings

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class JarvisHealthService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._started_at = time.monotonic()

    def snapshot(self, *, application: Any | None = None) -> dict[str, Any]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            return self._base_snapshot(application=application, providers_payload=self._providers_snapshot_sync())
        return asyncio.run(self.snapshot_async(application=application))

    async def snapshot_async(self, *, application: Any | None = None) -> dict[str, Any]:
        providers_payload = await self._providers_snapshot_async()
        return self._base_snapshot(application=application, providers_payload=providers_payload)

    def _base_snapshot(self, *, application: Any | None, providers_payload: dict[str, Any]) -> dict[str, Any]:
        process_payload = self._process_snapshot()
        runtime_payload = self._runtime_snapshot(application)

        overall_status = "ok"
        if process_payload.get("status") == "degraded":
            overall_status = "degraded"
        if any(item.get("status") == "error" for item in providers_payload.values()):
            overall_status = "degraded"

        return {
            "status": overall_status,
            "environment": self._settings.environment,
            "debug": self._settings.debug,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(time.monotonic() - self._started_at, 3),
            "paths": {
                "project_root": str(self._settings.project_root),
                "logs_root": str(self._settings.logs_root),
                "memory_db_path": str(self._settings.memory_db_path),
            },
            "process": process_payload,
            "runtime": runtime_payload,
            "providers": providers_payload,
            "limits": {
                "max_concurrent_requests": self._settings.resources.max_concurrent_requests,
                "max_pending_requests": self._settings.resources.max_pending_requests,
                "max_memory_mb": self._settings.resources.max_memory_mb,
                "memory_warning_mb": self._settings.health.memory_warning_mb,
            },
        }

    def _process_snapshot(self) -> dict[str, Any]:
        if psutil is None:
            return {
                "status": "unknown",
                "pid": os.getpid(),
                "memory_rss_mb": None,
                "memory_percent": None,
                "thread_count": None,
                "note": "psutil_not_installed",
            }

        process = psutil.Process()
        rss_mb = round(process.memory_info().rss / (1024 * 1024), 2)
        memory_percent = round(process.memory_percent(), 2)
        status = "ok"
        if rss_mb >= self._settings.resources.max_memory_mb:
            status = "degraded"
        elif rss_mb >= self._settings.health.memory_warning_mb:
            status = "degraded"

        return {
            "status": status,
            "pid": process.pid,
            "memory_rss_mb": rss_mb,
            "memory_percent": memory_percent,
            "thread_count": process.num_threads(),
            "open_files": len(process.open_files()) if hasattr(process, "open_files") else None,
        }

    def _runtime_snapshot(self, application: Any | None) -> dict[str, Any]:
        if application is None:
            return {
                "active_requests": 0,
                "busy": False,
            }

        active_requests = 0
        busy = False
        try:
            busy = bool(getattr(application.orchestrator, "is_busy", False))
        except Exception:
            busy = False
        try:
            active_requests = len(tuple(getattr(application.orchestrator, "_active_requests", ()) or ()))
        except Exception:
            active_requests = int(busy)

        pending_reminders = 0
        try:
            pending_reminders = int(getattr(application.orchestrator, "_scheduler").pending_count)
        except Exception:
            pending_reminders = 0

        return {
            "active_requests": active_requests,
            "busy": busy,
            "pending_reminders": pending_reminders,
            "safe_mode": getattr(application, "settings", self._settings).safe_mode,
            "offline_mode": getattr(application, "settings", self._settings).offline_mode,
            "simulate_actions": getattr(application, "settings", self._settings).simulate_actions,
        }

    def _providers_snapshot_sync(self) -> dict[str, Any]:
        timeout = self._settings.health.api_timeout_seconds
        return {
            "openai": self._provider_connectivity_sync(
                name="openai",
                configured=bool(self._settings.providers.openai_api_key),
                url=self._settings.providers.openai_base_url.rstrip("/") + "/models",
                headers={
                    "Authorization": f"Bearer {self._settings.providers.openai_api_key}",
                } if self._settings.providers.openai_api_key else None,
                timeout=timeout,
            ),
            "anthropic": self._provider_connectivity_sync(
                name="anthropic",
                configured=bool(self._settings.providers.anthropic_api_key),
                url="https://api.anthropic.com",
                headers=None,
                timeout=timeout,
            ),
            "ollama": self._provider_connectivity_sync(
                name="ollama",
                configured=bool(self._settings.providers.ollama_url),
                url=self._settings.providers.ollama_url.rstrip("/") + "/api/tags",
                headers=None,
                timeout=timeout,
            ),
        }

    async def _providers_snapshot_async(self) -> dict[str, Any]:
        timeout = self._settings.health.api_timeout_seconds
        results = await asyncio.gather(
            self._provider_connectivity_async(
                name="openai",
                configured=bool(self._settings.providers.openai_api_key),
                url=self._settings.providers.openai_base_url.rstrip("/") + "/models",
                headers={
                    "Authorization": f"Bearer {self._settings.providers.openai_api_key}",
                } if self._settings.providers.openai_api_key else None,
                timeout=timeout,
            ),
            self._provider_connectivity_async(
                name="anthropic",
                configured=bool(self._settings.providers.anthropic_api_key),
                url="https://api.anthropic.com",
                headers=None,
                timeout=timeout,
            ),
            self._provider_connectivity_async(
                name="ollama",
                configured=bool(self._settings.providers.ollama_url),
                url=self._settings.providers.ollama_url.rstrip("/") + "/api/tags",
                headers=None,
                timeout=timeout,
            ),
        )
        return {"openai": results[0], "anthropic": results[1], "ollama": results[2]}

    def _provider_connectivity_sync(
        self,
        *,
        name: str,
        configured: bool,
        url: str,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> dict[str, Any]:
        payload = {
            "status": "unknown",
            "configured": configured,
            "url": url,
        }
        if not configured:
            payload["status"] = "disabled"
            return payload
        if not self._settings.health.check_external_connectivity and name != "ollama":
            payload["status"] = "configured"
            return payload

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(url, headers=headers)
            payload.update(self._response_payload(response))
        except httpx.HTTPError as exc:
            payload["status"] = "error"
            payload["error"] = str(exc)
        return payload

    async def _provider_connectivity_async(
        self,
        *,
        name: str,
        configured: bool,
        url: str,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> dict[str, Any]:
        payload = {
            "status": "unknown",
            "configured": configured,
            "url": url,
        }
        if not configured:
            payload["status"] = "disabled"
            return payload
        if not self._settings.health.check_external_connectivity and name != "ollama":
            payload["status"] = "configured"
            return payload
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers)
            payload.update(self._response_payload(response))
        except httpx.HTTPError as exc:
            payload["status"] = "error"
            payload["error"] = str(exc)
        return payload

    @staticmethod
    def _response_payload(response: httpx.Response) -> dict[str, Any]:
        status = "ok" if response.status_code < 500 else "error"
        if response.status_code in {401, 403, 404}:
            status = "configured"
        return {
            "status": status,
            "status_code": response.status_code,
        }
