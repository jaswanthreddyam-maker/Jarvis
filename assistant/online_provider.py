"""Online LLM provider — queries external APIs for knowledge/reasoning tasks.

Supports: OpenAI, Claude (Anthropic), Ollama (local fallback).
Gracefully degrades to local fallback when APIs are unreachable.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any

import anthropic
import openai
import requests
from requests.exceptions import Timeout

logger = logging.getLogger("Jarvis.OnlineProvider")

_LOCAL_GREETING_REPLIES = [
    "Hey! What can I do for you?",
    "Hello! I'm listening.",
    "Hi there! What do you need?",
    "Hey, I'm here. What's up?",
    "Hello! Ready when you are.",
]

_GREETING_TRIGGERS = {
    "hey", "hi", "hello", "hey jarvis", "hi jarvis",
    "hello jarvis", "good morning", "good evening",
    "good afternoon", "what's up", "sup", "yo", "howdy"
}


@dataclass(slots=True)
class OnlineResponse:
    text: str
    provider: str          # "openai", "anthropic", "ollama", "fallback"
    tokens_used: int = 0
    latency_ms: float = 0.0
    success: bool = True
    error: str = ""


class OnlineProvider:
    """Query online LLMs with automatic fallback chain.

    Priority:
        1. OpenAI (if OPENAI_API_KEY set)
        2. Anthropic/Claude (if ANTHROPIC_API_KEY set)
        3. Ollama local (if running)
        4. Graceful fallback message
    """

    MAX_TOKENS = 512

    def __init__(self) -> None:
        self._openai_key = os.getenv("OPENAI_API_KEY", "")
        self._anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        self._ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        self._system_prompt = (
            "You are Jarvis, a concise and helpful AI desktop assistant. "
            "Keep responses clear and under 3 sentences when possible. "
            "Be conversational but professional."
        )

    def query(self, user_text: str, context: list[dict] | None = None) -> OnlineResponse:
        """Try providers in priority order until one succeeds."""
        # Fast-path: greetings never need an API call
        normalized = user_text.strip().lower().rstrip(".,! ")
        if normalized in _GREETING_TRIGGERS:
            return OnlineResponse(
                text=random.choice(_LOCAL_GREETING_REPLIES),
                provider="local",
                tokens_used=0,
                latency_ms=0.0,
                success=True,
            )

        start = time.perf_counter()

        # 1. OpenAI
        if self._openai_key:
            result = self._try_openai(user_text, context)
            if result.success:
                result.latency_ms = (time.perf_counter() - start) * 1000
                return result

        # 2. Anthropic
        if self._anthropic_key:
            result = self._try_anthropic(user_text, context)
            if result.success:
                result.latency_ms = (time.perf_counter() - start) * 1000
                return result

        # 3. Ollama (local large model)
        result = self._try_ollama(user_text, context)
        if result.success:
            result.latency_ms = (time.perf_counter() - start) * 1000
            return result

        # 4. Fallback
        elapsed = (time.perf_counter() - start) * 1000
        return OnlineResponse(
            text="I'll handle this locally. I couldn't reach any online AI service right now.",
            provider="fallback",
            latency_ms=elapsed,
            success=False,
            error="all_providers_unavailable",
        )

    def _try_openai(self, text: str, context: list[dict] | None) -> OnlineResponse:
        try:
            messages = [{"role": "system", "content": self._system_prompt}]
            if context:
                messages.extend(context)
            messages.append({"role": "user", "content": text})

            client = openai.OpenAI(api_key=self._openai_key)
            response = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=messages,
                max_tokens=self.MAX_TOKENS,
                temperature=0.7,
                timeout=30,
            )
            content = response.choices[0].message.content
            tokens = response.usage.total_tokens if response.usage else 0
            logger.info("OpenAI response: %d tokens", tokens)
            return OnlineResponse(
                text=(content or "").strip(),
                provider="openai",
                tokens_used=tokens,
            )
        except openai.APITimeoutError as e:
            logger.warning("OpenAI timeout: %s", e)
            return OnlineResponse(
                text="I'm having trouble reaching my knowledge service right now. Try again in a moment.",
                provider="fallback",
                success=False,
                error="timeout",
            )
        except Exception as e:
            logger.warning("OpenAI failed: %s", e)
            return OnlineResponse(text="", provider="openai", success=False, error=str(e))

    def _try_anthropic(self, text: str, context: list[dict] | None) -> OnlineResponse:
        try:
            messages = []
            if context:
                messages.extend(context)
            messages.append({"role": "user", "content": text})

            client = anthropic.Anthropic(api_key=self._anthropic_key)
            response = client.messages.create(
                model=os.getenv("ANTHROPIC_MODEL", "claude-3-haiku-20240307"),
                system=self._system_prompt,
                messages=messages,
                max_tokens=self.MAX_TOKENS,
                timeout=30,
            )
            content = response.content[0].text
            tokens = response.usage.input_tokens + response.usage.output_tokens
            logger.info("Anthropic response: %d tokens", tokens)
            return OnlineResponse(
                text=content.strip(),
                provider="anthropic",
                tokens_used=tokens,
            )
        except anthropic.APITimeoutError as e:
            logger.warning("Anthropic timeout: %s", e)
            return OnlineResponse(
                text="I'm having trouble reaching my knowledge service right now. Try again in a moment.",
                provider="fallback",
                success=False,
                error="timeout",
            )
        except Exception as e:
            logger.warning("Anthropic failed: %s", e)
            return OnlineResponse(text="", provider="anthropic", success=False, error=str(e))

    def _try_ollama(self, text: str, context: list[dict] | None) -> OnlineResponse:
        try:

            messages = [{"role": "system", "content": self._system_prompt}]
            if context:
                messages.extend(context)
            messages.append({"role": "user", "content": text})

            resp = requests.post(
                f"{self._ollama_url}/api/chat",
                json={
                    "model": os.getenv("OLLAMA_MODEL", "llama3"),
                    "messages": messages,
                    "stream": False,
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            logger.info("Ollama response received")
            return OnlineResponse(
                text=content.strip(),
                provider="ollama",
            )
        except Timeout as e:
            logger.warning("Ollama timeout: %s", e)
            return OnlineResponse(
                text="I'm having trouble reaching my knowledge service right now. Try again in a moment.",
                provider="fallback",
                success=False,
                error="timeout",
            )
        except Exception as e:
            logger.debug("Ollama not available: %s", e)
            return OnlineResponse(text="", provider="ollama", success=False, error=str(e))
