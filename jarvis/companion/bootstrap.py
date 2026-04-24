"""
Companion Bootstrap — builds and wires all companion subsystems together.

This is the single entry point for creating a fully configured companion
instance from Settings + existing Jarvis application components.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from jarvis.companion.context_engine import ContextEngine
from jarvis.companion.gateway import VoiceGateway
from jarvis.companion.memory_system import CompanionMemory
from jarvis.companion.persona import CompanionPersona, PersonaTraits
from jarvis.companion.session import CompanionSession, SessionConfig
from jarvis.companion.voice_pipeline import VoiceConfig

logger = logging.getLogger("Companion.Bootstrap")


def build_voice_config(settings: Any) -> VoiceConfig:
    """Build VoiceConfig from Jarvis Settings."""
    audio = getattr(settings, "audio", {}) or {}
    models = getattr(settings, "models", {}) or {}
    providers = getattr(settings, "providers", None)

    openai_key = ""
    openai_base = "https://api.openai.com/v1"
    if providers:
        openai_key = getattr(providers, "openai_api_key", "") or ""
        openai_base = getattr(providers, "openai_base_url", openai_base) or openai_base

    # Also check env directly
    if not openai_key:
        openai_key = os.environ.get("OPENAI_API_KEY", "")

    # Decide providers based on available keys
    stt_provider = "openai_api" if openai_key else "whisper_local"
    tts_provider = "openai_api" if openai_key else "pyttsx3"

    return VoiceConfig(
        stt_provider=stt_provider,
        whisper_model=str(models.get("asr", "base.en")),
        tts_provider=tts_provider,
        tts_voice=os.environ.get("COMPANION_TTS_VOICE", "nova"),
        tts_model=os.environ.get("COMPANION_TTS_MODEL", "tts-1"),
        tts_speed=float(os.environ.get("COMPANION_TTS_SPEED", "1.0")),
        sample_rate=int(audio.get("sample_rate", 16000)),
        silence_threshold=float(audio.get("vad_threshold", 0.01)),
        silence_duration_s=float(audio.get("silence_duration", 1.5)),
        openai_api_key=openai_key,
        openai_base_url=openai_base,
        deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
    )


def build_persona(settings: Any) -> CompanionPersona:
    """Build CompanionPersona from settings and config files."""
    personality_path = getattr(settings, "personality_path", None)
    config_root = getattr(settings, "config_root", None)

    # Try loading persona config
    persona_config_path = None
    if config_root:
        candidate = Path(config_root) / "companion_persona.json"
        if candidate.exists():
            persona_config_path = candidate

    return CompanionPersona(config_path=persona_config_path)


def build_companion_memory(settings: Any, legacy_memory: Any = None) -> CompanionMemory:
    """Build CompanionMemory with bridge to existing Jarvis memory."""
    data_dir = Path(getattr(settings, "project_root", ".")) / "data" / "companion"
    return CompanionMemory(data_dir=data_dir, existing_memory_manager=legacy_memory)


def build_session(
    *,
    settings: Any,
    persona: CompanionPersona,
    memory: CompanionMemory,
    llm_generate: Any = None,
    voice_mode: bool = True,
) -> CompanionSession:
    """Build a single CompanionSession."""
    voice_config = build_voice_config(settings)
    session_config = SessionConfig(
        voice=voice_config,
        voice_mode=voice_mode,
        max_idle_seconds=float(os.environ.get("COMPANION_IDLE_TIMEOUT", "300")),
    )
    return CompanionSession(
        persona=persona,
        memory=memory,
        config=session_config,
        llm_generate=llm_generate,
    )


def build_gateway(
    *,
    settings: Any,
    persona: CompanionPersona,
    memory: CompanionMemory,
    llm_generate: Any = None,
    host: str = "0.0.0.0",
    port: int = 8766,
) -> VoiceGateway:
    """Build the WebSocket voice gateway."""

    def session_factory() -> CompanionSession:
        return build_session(
            settings=settings,
            persona=persona,
            memory=memory,
            llm_generate=llm_generate,
        )

    return VoiceGateway(
        session_factory=session_factory,
        host=host,
        port=port,
    )


async def build_llm_handler(application: Any) -> Any:
    """
    Create an async LLM handler that bridges the companion session
    to the existing Jarvis application's LLM pipeline.
    """

    async def generate(messages: list[dict[str, str]]) -> str:
        """Generate a response using the Jarvis LLM pipeline."""
        # Extract the user message (last one)
        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_text = msg.get("content", "")
                break

        if not user_text:
            return ""

        # Use the existing Jarvis application pipeline
        if hasattr(application, "handle_text_async"):
            try:
                response, _ = await application.handle_text_async(user_text)
                return response
            except Exception as exc:
                logger.warning("Jarvis pipeline failed, falling back: %s", exc)

        # Direct LLM call fallback
        if hasattr(application, "orchestrator"):
            orch = application.orchestrator
            if hasattr(orch, "_brain") and hasattr(orch._brain, "_llm_client"):
                client = orch._brain._llm_client
                if hasattr(client, "generate_chat"):
                    try:
                        result = await asyncio.to_thread(client.generate_chat, messages)
                        return str(result)
                    except Exception:
                        pass

        return "I'm having trouble connecting to my brain right now. Give me a moment."

    return generate
