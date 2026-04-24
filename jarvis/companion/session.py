"""
Companion Session — manages the lifecycle of a voice companion session.

A session ties together:
  - Voice pipeline (STT/TTS)
  - Memory retrieval & recording
  - Context interpretation
  - Persona adaptation
  - LLM response generation

This is the main orchestration point for the companion experience.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from jarvis.companion.context_engine import ContextEngine, ContextFrame
from jarvis.companion.memory_system import CompanionMemory
from jarvis.companion.persona import CompanionPersona
from jarvis.companion.voice_pipeline import PipelineState, VoiceConfig, VoicePipeline

logger = logging.getLogger("Companion.Session")


@dataclass(slots=True)
class SessionConfig:
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    max_idle_seconds: float = 300.0        # auto-end after 5min idle
    enable_memory_consolidation: bool = True
    enable_proactive_memory: bool = True     # reference past memories proactively
    voice_mode: bool = True


@dataclass(slots=True)
class SessionMetrics:
    session_id: str = ""
    start_time: float = 0.0
    turn_count: int = 0
    total_stt_ms: float = 0.0
    total_llm_ms: float = 0.0
    total_tts_ms: float = 0.0
    interruptions: int = 0
    avg_latency_ms: float = 0.0


class CompanionSession:
    """
    Manages a single companion interaction session. Coordinates all subsystems
    to produce a deeply human-feeling voice conversation.
    """

    def __init__(
        self,
        *,
        persona: CompanionPersona,
        memory: CompanionMemory,
        config: SessionConfig | None = None,
        llm_generate: Callable[..., Any] | None = None,
    ) -> None:
        self._config = config or SessionConfig()
        self._session_id = uuid4().hex
        self._persona = persona
        self._memory = memory
        self._context_engine = ContextEngine()
        self._llm_generate = llm_generate

        # Build voice pipeline with LLM handler wired in
        self._pipeline = VoicePipeline(
            self._config.voice,
            llm_handler=self._handle_user_input,
        )

        self._is_active = False
        self._last_activity = time.time()
        self._metrics = SessionMetrics(session_id=self._session_id, start_time=time.time())

        # Callbacks for external UI
        self._on_state_change: Callable[[str, dict[str, Any]], None] | None = None
        self._on_turn_complete: Callable[[str, str, ContextFrame], None] | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_active(self) -> bool:
        return self._is_active

    @property
    def metrics(self) -> SessionMetrics:
        return self._metrics

    @property
    def pipeline(self) -> VoicePipeline:
        return self._pipeline

    def set_callbacks(
        self,
        *,
        on_state_change: Callable[[str, dict[str, Any]], None] | None = None,
        on_turn_complete: Callable[[str, str, ContextFrame], None] | None = None,
    ) -> None:
        self._on_state_change = on_state_change
        self._on_turn_complete = on_turn_complete

    async def start(self) -> None:
        """Initialize the session and greet the user."""
        self._is_active = True
        self._last_activity = time.time()
        logger.info("Session %s started", self._session_id)

        # Set up pipeline callbacks
        self._pipeline.set_callbacks(
            on_state_change=self._on_pipeline_state,
            on_transcript=lambda t: logger.info("User: %s", t),
            on_response=lambda r: logger.info("Companion: %s", r),
        )

        # Generate personalized greeting
        greeting = self._build_greeting()
        if self._config.voice_mode:
            voice_style = self._persona.state.current_voice_style.value
            await self._pipeline.speak(greeting, voice_style=voice_style)

        self._memory.add_turn("assistant", greeting)
        self._emit_state("session_started", {"greeting": greeting})

    async def run_voice_loop(self) -> None:
        """Main voice interaction loop. Runs until session ends."""
        if not self._config.voice_mode:
            return

        while self._is_active:
            # Check idle timeout
            if time.time() - self._last_activity > self._config.max_idle_seconds:
                await self._end_with_farewell("You've been quiet for a while. I'll be here when you need me.")
                break

            try:
                voice_style = self._persona.state.current_voice_style.value
                user_text, response = await self._pipeline.listen_and_respond(voice_style=voice_style)

                if not user_text:
                    await asyncio.sleep(0.1)
                    continue

                self._last_activity = time.time()
                self._metrics.turn_count += 1

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Voice loop error: %s", exc)
                await asyncio.sleep(1.0)

    async def handle_text_input(self, text: str) -> str:
        """Handle text input (for hybrid text+voice mode)."""
        self._last_activity = time.time()
        response = await self._handle_user_input(text)
        self._metrics.turn_count += 1
        return response

    async def end(self) -> dict[str, Any]:
        """End the session and consolidate memory."""
        self._is_active = False
        self._pipeline.stop()
        self._context_engine.reset_session()

        # Consolidate session memories
        consolidated = []
        if self._config.enable_memory_consolidation:
            consolidated = self._memory.end_session(session_id=self._session_id)

        self._emit_state("session_ended", {
            "metrics": {
                "turn_count": self._metrics.turn_count,
                "duration_s": time.time() - self._metrics.start_time,
                "interruptions": self._metrics.interruptions,
            },
            "memories_created": len(consolidated),
        })

        logger.info(
            "Session %s ended - %d turns, %d memories consolidated",
            self._session_id, self._metrics.turn_count, len(consolidated),
        )

        return {
            "session_id": self._session_id,
            "metrics": self._metrics,
            "consolidated_memories": consolidated,
        }

    async def _handle_user_input(self, text: str) -> str:
        """
        Core pipeline: interpret context → retrieve memory → adapt persona →
        generate response → record exchange.
        """
        # 1. Interpret context signals
        unresolved = [t["topic"] for t in self._memory.working.unresolved_threads()]
        frame = self._context_engine.interpret(
            text,
            unresolved_threads=unresolved,
            voice_mode=self._config.voice_mode,
        )

        # 2. Adapt persona based on signals
        self._persona.adapt_tone(frame.as_signals())

        # 3. Retrieve relevant memories
        memory_context = self._memory.retrieve_for_response(text)

        # 4. Build system prompt with persona + memory
        system_prompt = self._persona.build_system_prompt(
            memory_context=memory_context,
            active_signals=frame.as_signals(),
        )

        # 5. Build conversation messages
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self._memory.working.conversation_for_llm(limit=10))
        messages.append({"role": "user", "content": text})

        # 6. Generate LLM response
        response = await self._generate_response(messages, frame)

        # 7. Record the exchange
        self._memory.record_exchange(text, response, emotion=frame.sentiment)

        # 8. Handle memory-worthy content
        if frame.should_remember:
            self._memory.remember_fact(text)

        # 9. Update persona rapport
        if frame.sentiment in ("grateful", "happy"):
            self._persona.update_rapport(0.05)
        elif frame.sentiment in ("frustrated",):
            self._persona.update_rapport(-0.02)

        # 10. Emit turn completion
        if self._on_turn_complete:
            self._on_turn_complete(text, response, frame)

        return response

    async def _generate_response(self, messages: list[dict[str, str]], frame: ContextFrame) -> str:
        """Generate response using the configured LLM."""
        if self._llm_generate:
            try:
                start = time.time()
                result = await self._llm_generate(messages)
                self._metrics.total_llm_ms += (time.time() - start) * 1000
                if isinstance(result, str):
                    return result
                if isinstance(result, tuple) and result:
                    return str(result[0])
                return str(result)
            except Exception as exc:
                logger.exception("LLM generation failed: %s", exc)
                return self._fallback_response(frame)
        return self._fallback_response(frame)

    def _fallback_response(self, frame: ContextFrame) -> str:
        """Fallback when LLM is unavailable."""
        if frame.is_question:
            return "I'm having trouble thinking right now. Can you ask me again in a moment?"
        if frame.is_emotional:
            return "I hear you. Give me a second, I'm having a brief moment."
        return "I'm here, just processing. One moment."

    def _build_greeting(self) -> str:
        """Generate a context-aware greeting."""
        tod = self._context_engine._detect_time_of_day()
        name = self._persona.traits.name

        # Check for returning user memories
        recent_episodes = self._memory.episodic.all_episodes(limit=1)

        if recent_episodes:
            return {
                "morning": f"Good morning! It's good to hear from you again.",
                "afternoon": f"Hey, welcome back! How's your afternoon going?",
                "evening": f"Good evening! Nice to catch up again.",
                "late_night": f"Hey, burning the midnight oil again?",
                "early_morning": f"You're up early! Everything okay?",
            }.get(tod, "Hey, good to see you again!")
        else:
            return {
                "morning": f"Good morning! I'm {name}. Nice to meet you.",
                "afternoon": f"Hey there! I'm {name}. What's on your mind?",
                "evening": f"Good evening! I'm {name}. How can I help tonight?",
                "late_night": f"Hey, I'm {name}. Looks like we're both night owls.",
                "early_morning": f"Morning! I'm {name}. What brings you here so early?",
            }.get(tod, f"Hi! I'm {name}. Let's chat.")

    async def _end_with_farewell(self, message: str) -> None:
        if self._config.voice_mode:
            await self._pipeline.speak(message, voice_style="soft")
        self._memory.add_turn("assistant", message)
        await self.end()

    def _on_pipeline_state(self, state: PipelineState) -> None:
        self._emit_state("pipeline_state", {"state": state.value})
        if state == PipelineState.INTERRUPTED:
            self._metrics.interruptions += 1

    def _emit_state(self, event: str, data: dict[str, Any]) -> None:
        if self._on_state_change:
            try:
                self._on_state_change(event, data)
            except Exception as exc:
                logger.debug("State callback error (non-fatal): %s", exc)
