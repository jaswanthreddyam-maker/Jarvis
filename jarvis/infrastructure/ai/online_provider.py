from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from jarvis.config.settings import load_settings
from jarvis.core.retry import RetryPolicy, retry_call


logger = logging.getLogger("Jarvis.OnlineProvider")


@dataclass(slots=True)
class OnlineResponse:
    text: str
    provider: str
    tokens_used: int = 0
    latency_ms: float = 0.0
    success: bool = True
    error: str = ""


class OnlineProvider:
    def __init__(self) -> None:
        self._settings = load_settings()
        self._timeout_seconds = self._settings.providers.request_timeout_seconds
        self._max_tokens = self._settings.providers.max_tokens
        self._retry_policy = RetryPolicy(
            max_attempts=self._settings.retry.max_attempts,
            backoff_base_seconds=self._settings.retry.backoff_base_seconds,
            backoff_max_seconds=self._settings.retry.backoff_max_seconds,
            jitter_seconds=self._settings.retry.jitter_seconds,
        )
        self._system_prompt = (
            "You are Jarvis, a concise and helpful AI desktop assistant. "
            "Keep responses clear and under 3 sentences when possible."
        )

    def query(
        self,
        user_text: str,
        context: list[dict[str, Any]] | None = None,
        *,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        response_format: str | None = None,
    ) -> OnlineResponse:
        started_at = time.perf_counter()
        resolved_prompt = system_prompt or self._system_prompt

        providers = (
            self._try_openai,
            self._try_anthropic,
            self._try_ollama,
        )
        for provider in providers:
            response = provider(
                user_text,
                context,
                system_prompt=resolved_prompt,
                temperature=temperature,
                response_format=response_format,
            )
            if response.success:
                response.latency_ms = (time.perf_counter() - started_at) * 1000.0
                return response

        return OnlineResponse(
            text="I couldn't reach any online reasoning service right now.",
            provider="unavailable",
            latency_ms=(time.perf_counter() - started_at) * 1000.0,
            success=False,
            error="all_providers_unavailable",
        )

    @staticmethod
    def _should_retry_provider_error(exc: BaseException) -> bool:
        message = f"{type(exc).__name__} {exc}".lower()
        retry_tokens = ("timeout", "tempor", "connection", "rate", "429", "500", "502", "503", "504", "server")
        return any(token in message for token in retry_tokens)

    def _try_openai(
        self,
        text: str,
        context: list[dict[str, Any]] | None,
        *,
        system_prompt: str,
        temperature: float,
        response_format: str | None,
    ) -> OnlineResponse:
        api_key = self._settings.providers.openai_api_key
        if not api_key:
            return OnlineResponse(text="", provider="openai", success=False, error="openai_not_configured")
        try:
            def _call():
                messages = [{"role": "system", "content": system_prompt}, *(context or []), {"role": "user", "content": text}]
                payload: dict[str, Any] = {
                    "model": self._settings.providers.openai_model,
                    "messages": messages,
                    "max_tokens": self._max_tokens,
                    "temperature": temperature,
                }
                if response_format == "json_object":
                    payload["response_format"] = {"type": "json_object"}
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.post(
                        self._settings.providers.openai_base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json=payload,
                    )
                    response.raise_for_status()
                    return response

            response = retry_call(
                _call,
                operation_name="openai.chat.completions",
                policy=self._retry_policy,
                logger=logger,
                retryable_exceptions=(httpx.HTTPError,),
                should_retry=self._should_retry_provider_error,
            )
            data = response.json()
            content = str((data.get("choices") or [{}])[0].get("message", {}).get("content", "") or "").strip()
            usage = dict(data.get("usage") or {})
            return OnlineResponse(
                text=content,
                provider="openai",
                tokens_used=int(usage.get("total_tokens", 0) or 0),
            )
        except Exception as exc:
            return OnlineResponse(text="", provider="openai", success=False, error=str(exc))

    def _try_anthropic(
        self,
        text: str,
        context: list[dict[str, Any]] | None,
        *,
        system_prompt: str,
        temperature: float,
        response_format: str | None,
    ) -> OnlineResponse:
        del response_format
        api_key = self._settings.providers.anthropic_api_key
        if not api_key:
            return OnlineResponse(text="", provider="anthropic", success=False, error="anthropic_not_configured")
        try:
            def _call():
                payload = {
                    "model": self._settings.providers.anthropic_model,
                    "system": system_prompt,
                    "messages": [*(context or []), {"role": "user", "content": text}],
                    "max_tokens": self._max_tokens,
                    "temperature": temperature,
                }
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": api_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        json=payload,
                    )
                    response.raise_for_status()
                    return response

            response = retry_call(
                _call,
                operation_name="anthropic.messages",
                policy=self._retry_policy,
                logger=logger,
                retryable_exceptions=(httpx.HTTPError,),
                should_retry=self._should_retry_provider_error,
            )
            data = response.json()
            content_block = (data.get("content") or [{}])[0]
            usage = dict(data.get("usage") or {})
            return OnlineResponse(
                text=str(content_block.get("text", "") or "").strip(),
                provider="anthropic",
                tokens_used=int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0),
            )
        except Exception as exc:
            return OnlineResponse(text="", provider="anthropic", success=False, error=str(exc))

    def _try_ollama(
        self,
        text: str,
        context: list[dict[str, Any]] | None,
        *,
        system_prompt: str,
        temperature: float,
        response_format: str | None,
    ) -> OnlineResponse:
        try:
            def _call():
                payload: dict[str, Any] = {
                    "model": self._settings.providers.ollama_model,
                    "messages": [{"role": "system", "content": system_prompt}, *(context or []), {"role": "user", "content": text}],
                    "stream": False,
                    "options": {"temperature": temperature, "num_predict": self._max_tokens},
                }
                if response_format == "json_object":
                    payload["format"] = "json"
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.post(self._settings.providers.ollama_url.rstrip("/") + "/api/chat", json=payload)
                    response.raise_for_status()
                    return response

            response = retry_call(
                _call,
                operation_name="ollama.api.chat",
                policy=self._retry_policy,
                logger=logger,
                retryable_exceptions=(httpx.HTTPError,),
                should_retry=self._should_retry_provider_error,
            )
            data = response.json()
            return OnlineResponse(
                text=str(data.get("message", {}).get("content", "") or "").strip(),
                provider="ollama",
            )
        except Exception as exc:
            return OnlineResponse(text="", provider="ollama", success=False, error=str(exc))
