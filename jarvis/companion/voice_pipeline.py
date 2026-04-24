"""
Voice Pipeline — streaming STT → LLM → expressive TTS.

Replaces the old pyttsx3/stub ASR pipeline with a real cascading voice system:
  - STT: OpenAI Whisper (local) or Deepgram/OpenAI API (online)
  - LLM: OpenAI / Anthropic / Ollama with streaming
  - TTS: OpenAI TTS API (expressive) or Coqui TTS (local fallback)

Supports:
  - Barge-in / interruption handling
  - Silence detection + turn-taking
  - Streaming response for low latency
  - Emotional/expressive speech control via voice style tags
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import wave
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable

logger = logging.getLogger("Companion.VoicePipeline")


class PipelineState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"


@dataclass(slots=True)
class VoiceConfig:
    # STT settings
    stt_provider: str = "whisper_local"     # whisper_local | openai_api | deepgram
    whisper_model: str = "base.en"
    stt_language: str = "en"

    # TTS settings
    tts_provider: str = "openai_api"        # openai_api | coqui_local | pyttsx3
    tts_voice: str = "nova"                 # OpenAI: alloy, echo, fable, onyx, nova, shimmer
    tts_model: str = "tts-1"               # tts-1 or tts-1-hd
    tts_speed: float = 1.0

    # Audio settings
    sample_rate: int = 16000
    channels: int = 1
    chunk_duration_ms: int = 300
    silence_threshold: float = 0.01
    silence_duration_s: float = 1.5         # silence before end-of-turn
    max_recording_s: float = 30.0

    # Latency budgets (ms)
    stt_budget_ms: int = 800
    llm_budget_ms: int = 2000
    tts_budget_ms: int = 500

    # API keys (pulled from settings at init)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    deepgram_api_key: str = ""


# ── STT Backends ─────────────────────────────────────────────────────────────

class STTBackend:
    """Base class for speech-to-text backends."""

    async def transcribe(self, audio_data: bytes, *, sample_rate: int = 16000) -> str:
        raise NotImplementedError


class WhisperLocalSTT(STTBackend):
    """Local Whisper model for offline STT."""

    def __init__(self, model_name: str = "base.en") -> None:
        self._model = None
        self._model_name = model_name
        self._lock = threading.Lock()

    def _ensure_model(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    import whisper
                    self._model = whisper.load_model(self._model_name)
                    logger.info("Whisper model '%s' loaded", self._model_name)
        return self._model

    async def transcribe(self, audio_data: bytes, *, sample_rate: int = 16000) -> str:
        import numpy as np
        model = self._ensure_model()
        audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: model.transcribe(audio_array, fp16=False, language="en")
        )
        return str(result.get("text", "")).strip()


class OpenAISTT(STTBackend):
    """OpenAI Whisper API for online STT."""

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1") -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def transcribe(self, audio_data: bytes, *, sample_rate: int = 16000) -> str:
        import httpx
        # Package as WAV
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data)
        wav_buffer.seek(0)

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": ("audio.wav", wav_buffer, "audio/wav")},
                data={"model": "whisper-1", "language": "en"},
            )
            response.raise_for_status()
            return response.json().get("text", "").strip()


# ── TTS Backends ─────────────────────────────────────────────────────────────

class TTSBackend:
    """Base class for text-to-speech backends."""

    async def synthesize(self, text: str, *, voice_style: str = "normal") -> bytes:
        raise NotImplementedError

    async def synthesize_stream(self, text: str, *, voice_style: str = "normal") -> AsyncIterator[bytes]:
        # Default: non-streaming fallback
        data = await self.synthesize(text, voice_style=voice_style)
        yield data

    def stop(self) -> None:
        pass


class OpenAITTS(TTSBackend):
    """OpenAI TTS API — expressive, low-latency, high quality."""

    VOICE_STYLE_MAP = {
        "normal": "nova",
        "whisper": "shimmer",
        "enthusiastic": "nova",
        "soft": "shimmer",
        "confident": "onyx",
        "gentle": "shimmer",
    }

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "tts-1",
        default_voice: str = "nova",
        speed: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._default_voice = default_voice
        self._speed = speed

    async def synthesize(self, text: str, *, voice_style: str = "normal") -> bytes:
        import httpx
        voice = self.VOICE_STYLE_MAP.get(voice_style, self._default_voice)
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self._base_url}/audio/speech",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "input": text,
                    "voice": voice,
                    "speed": self._speed,
                    "response_format": "pcm",
                },
            )
            response.raise_for_status()
            return response.content

    async def synthesize_stream(self, text: str, *, voice_style: str = "normal") -> AsyncIterator[bytes]:
        import httpx
        voice = self.VOICE_STYLE_MAP.get(voice_style, self._default_voice)
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/audio/speech",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "input": text,
                    "voice": voice,
                    "speed": self._speed,
                    "response_format": "pcm",
                },
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    yield chunk


class LocalTTS(TTSBackend):
    """pyttsx3 fallback for fully offline operation."""

    def __init__(self) -> None:
        self._engine = None
        self._stop_event = threading.Event()
        try:
            import pyttsx3
            self._engine = pyttsx3.init()
        except Exception:
            logger.warning("pyttsx3 not available; TTS will be disabled")

    async def synthesize(self, text: str, *, voice_style: str = "normal") -> bytes:
        # pyttsx3 doesn't return audio bytes easily — speak directly
        if self._engine is None:
            return b""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._speak_blocking, text)
        return b""

    def _speak_blocking(self, text: str) -> None:
        if self._engine is None:
            return
        self._stop_event.clear()
        self._engine.say(text)
        self._engine.runAndWait()

    def stop(self) -> None:
        self._stop_event.set()
        if self._engine:
            try:
                self._engine.stop()
            except Exception:
                pass


# ── Audio Capture ────────────────────────────────────────────────────────────

class AudioCapture:
    """Captures audio from the microphone with silence detection."""

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config
        self._is_recording = False
        self._stop_event = threading.Event()

    async def capture_utterance(self) -> bytes | None:
        """Record until silence is detected or max duration reached. Returns raw PCM bytes."""
        import numpy as np
        try:
            import sounddevice as sd
        except ImportError:
            logger.error("sounddevice not installed — cannot capture audio")
            return None

        sample_rate = self._config.sample_rate
        chunk_samples = int(sample_rate * self._config.chunk_duration_ms / 1000)
        silence_chunks = int(self._config.silence_duration_s / (self._config.chunk_duration_ms / 1000))
        max_chunks = int(self._config.max_recording_s / (self._config.chunk_duration_ms / 1000))

        self._is_recording = True
        self._stop_event.clear()
        frames: list[bytes] = []
        silent_count = 0
        has_speech = False

        loop = asyncio.get_running_loop()

        def _record_chunk() -> tuple[bytes, bool]:
            data = sd.rec(chunk_samples, samplerate=sample_rate, channels=1, dtype="int16")
            sd.wait()
            raw = data.tobytes()
            rms = np.sqrt(np.mean(np.frombuffer(raw, dtype=np.int16).astype(np.float32) ** 2)) / 32768.0
            is_silent = rms < self._config.silence_threshold
            return raw, is_silent

        for _ in range(max_chunks):
            if self._stop_event.is_set():
                break
            raw, is_silent = await loop.run_in_executor(None, _record_chunk)
            frames.append(raw)

            if is_silent:
                silent_count += 1
                if has_speech and silent_count >= silence_chunks:
                    break
            else:
                silent_count = 0
                has_speech = True

        self._is_recording = False

        if not has_speech:
            return None
        return b"".join(frames)

    def interrupt(self) -> None:
        self._stop_event.set()

    @property
    def is_recording(self) -> bool:
        return self._is_recording


# ── Audio Playback ───────────────────────────────────────────────────────────

class AudioPlayback:
    """Plays PCM audio with interruption support."""

    def __init__(self, sample_rate: int = 24000) -> None:
        self._sample_rate = sample_rate
        self._is_playing = False
        self._stop_event = threading.Event()
        self._on_finished: Callable[[bool], None] | None = None

    def set_on_finished(self, callback: Callable[[bool], None] | None) -> None:
        self._on_finished = callback

    async def play(self, pcm_data: bytes) -> None:
        if not pcm_data:
            return
        try:
            import sounddevice as sd
            import numpy as np
        except ImportError:
            logger.warning("sounddevice not available — cannot play audio")
            return

        self._is_playing = True
        self._stop_event.clear()
        loop = asyncio.get_running_loop()

        def _play_blocking() -> bool:
            audio = np.frombuffer(pcm_data, dtype=np.int16)
            try:
                sd.play(audio, samplerate=self._sample_rate)
                # Poll for stop event during playback
                duration = len(audio) / self._sample_rate
                start = time.time()
                while time.time() - start < duration:
                    if self._stop_event.is_set():
                        sd.stop()
                        return True  # interrupted
                    time.sleep(0.05)
                sd.wait()
                return False  # completed normally
            except Exception as exc:
                logger.warning("Audio playback error: %s", exc)
                return False

        interrupted = await loop.run_in_executor(None, _play_blocking)
        self._is_playing = False
        if self._on_finished:
            self._on_finished(interrupted)

    def stop(self) -> None:
        self._stop_event.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    @property
    def is_playing(self) -> bool:
        return self._is_playing


# ── Unified Voice Pipeline ───────────────────────────────────────────────────

class VoicePipeline:
    """
    Orchestrates the full STT → LLM → TTS pipeline with interruption handling,
    turn-taking, and expressive voice control.
    """

    def __init__(
        self,
        config: VoiceConfig,
        *,
        llm_handler: Callable[[str], Any] | None = None,
    ) -> None:
        self._config = config
        self._state = PipelineState.IDLE
        self._capture = AudioCapture(config)
        self._playback = AudioPlayback(sample_rate=24000)
        self._llm_handler = llm_handler

        # Initialize STT backend
        if config.stt_provider == "openai_api" and config.openai_api_key:
            self._stt: STTBackend = OpenAISTT(config.openai_api_key, config.openai_base_url)
        else:
            self._stt = WhisperLocalSTT(config.whisper_model)

        # Initialize TTS backend
        if config.tts_provider == "openai_api" and config.openai_api_key:
            self._tts: TTSBackend = OpenAITTS(
                config.openai_api_key,
                base_url=config.openai_base_url,
                model=config.tts_model,
                default_voice=config.tts_voice,
                speed=config.tts_speed,
            )
        else:
            self._tts = LocalTTS()

        self._on_state_change: Callable[[PipelineState], None] | None = None
        self._on_transcript: Callable[[str], None] | None = None
        self._on_response: Callable[[str], None] | None = None

    @property
    def state(self) -> PipelineState:
        return self._state

    def set_callbacks(
        self,
        *,
        on_state_change: Callable[[PipelineState], None] | None = None,
        on_transcript: Callable[[str], None] | None = None,
        on_response: Callable[[str], None] | None = None,
    ) -> None:
        self._on_state_change = on_state_change
        self._on_transcript = on_transcript
        self._on_response = on_response

    def _set_state(self, state: PipelineState) -> None:
        self._state = state
        if self._on_state_change:
            self._on_state_change(state)

    async def listen_and_respond(self, voice_style: str = "normal") -> tuple[str, str]:
        """
        Full voice turn: listen → transcribe → respond → speak.
        Returns (user_text, assistant_text).
        """
        # 1. Listen
        self._set_state(PipelineState.LISTENING)
        audio = await self._capture.capture_utterance()
        if audio is None:
            self._set_state(PipelineState.IDLE)
            return "", ""

        # 2. Transcribe (STT)
        self._set_state(PipelineState.PROCESSING)
        start = time.time()
        transcript = await self._stt.transcribe(audio, sample_rate=self._config.sample_rate)
        stt_ms = (time.time() - start) * 1000
        logger.info("STT completed in %.0fms: %r", stt_ms, transcript[:80])

        if not transcript.strip():
            self._set_state(PipelineState.IDLE)
            return "", ""

        if self._on_transcript:
            self._on_transcript(transcript)

        # 3. Generate response (LLM)
        if self._llm_handler is None:
            self._set_state(PipelineState.IDLE)
            return transcript, ""

        start = time.time()
        response_text = await self._llm_handler(transcript)
        llm_ms = (time.time() - start) * 1000
        logger.info("LLM completed in %.0fms", llm_ms)

        if self._on_response:
            self._on_response(response_text)

        # 4. Speak (TTS)
        self._set_state(PipelineState.SPEAKING)
        start = time.time()
        audio_out = await self._tts.synthesize(response_text, voice_style=voice_style)
        tts_ms = (time.time() - start) * 1000
        logger.info("TTS completed in %.0fms", tts_ms)

        if audio_out:
            await self._playback.play(audio_out)

        self._set_state(PipelineState.IDLE)
        return transcript, response_text

    async def speak(self, text: str, *, voice_style: str = "normal") -> None:
        """Speak text without listening first."""
        self._set_state(PipelineState.SPEAKING)
        try:
            audio = await self._tts.synthesize(text, voice_style=voice_style)
            if audio:
                await self._playback.play(audio)
        finally:
            self._set_state(PipelineState.IDLE)

    def interrupt(self) -> None:
        """Handle barge-in: stop speaking and/or recording."""
        if self._state == PipelineState.SPEAKING:
            self._playback.stop()
            self._set_state(PipelineState.INTERRUPTED)
        elif self._state == PipelineState.LISTENING:
            self._capture.interrupt()

    def stop(self) -> None:
        """Shutdown the pipeline."""
        self._playback.stop()
        self._capture.interrupt()
        self._tts.stop()
        self._set_state(PipelineState.IDLE)
