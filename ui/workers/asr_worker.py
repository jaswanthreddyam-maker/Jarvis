from __future__ import annotations

import os
import time

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot

from assistant.runtime_config import get_audio_config


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ASRWorker(QObject):
    ready = Signal(object)
    transcript_ready = Signal(int, str)
    transcript_failed = Signal(int, str)
    wake_detected = Signal(str)
    log = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        audio_config = get_audio_config()
        self._model = None
        self._model_name = os.getenv(
            "JARVIS_WHISPER_MODEL",
            str(audio_config.get("whisper_model", "tiny.en")),
        )
        self._background_wake_enabled = _env_bool(
            "JARVIS_ENABLE_BACKGROUND_WAKE",
            bool(audio_config.get("background_wake_enabled", False)),
        )
        self._stream_decode_interval = float(
            os.getenv(
                "JARVIS_BACKGROUND_WAKE_INTERVAL",
                str(audio_config.get("background_wake_interval_seconds", 1.75)),
            )
        )
        self._stream_buffer: list[np.ndarray] = []
        self._is_transcribing = False
        self._last_stream_decode_at = 0.0

    @Slot()
    def load_model(self) -> None:
        started_at = time.perf_counter()
        try:
            if self._model is not None:
                self.ready.emit({"status": "Ready", "model": self._model_name})
                return
            try:
                import whisper
            except Exception as exc:
                self.ready.emit({"status": "Unavailable", "model": self._model_name, "error": str(exc)})
                return

            self.log.emit("ASR", f"Loading Whisper model '{self._model_name}'...")
            self._model = whisper.load_model(self._model_name)
            self.ready.emit({"status": "Ready", "model": self._model_name})
            self.log.emit("ASR", f"Whisper model '{self._model_name}' loaded.")
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            self.log.emit("Timing", f"ASR model load ({self._model_name}): {elapsed_ms:.1f} ms")
        except Exception as exc:
            self.ready.emit({"status": "Error", "model": self._model_name, "error": str(exc)})

    @Slot(bool)
    def set_background_wake_enabled(self, enabled: bool) -> None:
        self._background_wake_enabled = bool(enabled)
        if not enabled:
            self._stream_buffer.clear()
        state = "enabled" if enabled else "disabled"
        self.log.emit("ASR", f"Background wake transcription {state}.")

    @Slot(object)
    def on_audio_captured(self, chunk: object) -> None:
        try:
            if not self._background_wake_enabled:
                return
            self._stream_buffer.append(np.asarray(chunk, dtype=np.float32))

            if len(self._stream_buffer) >= 50 and not self._is_transcribing:
                now = time.perf_counter()
                if now - self._last_stream_decode_at < self._stream_decode_interval:
                    self._stream_buffer = self._stream_buffer[-25:]
                    return
                self._last_stream_decode_at = now
                self._is_transcribing = True
                audio = np.concatenate(self._stream_buffer)
                self._stream_buffer = self._stream_buffer[25:]
                self._process_stream(audio)
        except Exception as exc:
            print(f"[ASR] Stream error: {exc}")

    def _process_stream(self, audio: object) -> None:
        try:
            if self._model is None:
                self.load_model()
            
            # Fast transcription for wake word
            result = self._model.transcribe(audio, fp16=False)
            text = str(result.get("text", "")).strip().lower()
            
            if text:
                # Route live transcription to debug logs only
                self.log.emit("ASR_LOOP", f"transcription: {text}")
                if "jarvis" in text:
                    self.log.emit("ASR", "[ASR WAKE] detected keyword: jarvis")
                    import re
                    command = re.sub(r'\b(hey jarvis|hi jarvis|jarvis)\b', '', text, flags=re.IGNORECASE).strip()
                    command = re.sub(r'^[^a-z0-9]+', '', command).strip()
                    self.wake_detected.emit(command)
        except Exception as exc:
            self.log.emit("ASR", f"Stream process error: {exc}")
        finally:
            self._is_transcribing = False

    @Slot(object, int)
    def transcribe(self, payload: object, request_id: int) -> None:
        started_at = time.perf_counter()
        try:
            if self._model is None:
                self.load_model()
            if self._model is None:
                self.transcript_failed.emit(request_id, "Whisper is not available.")
                return

            if isinstance(payload, dict):
                data = payload
            else:
                data = {"audio": payload, "sample_rate": 16000}
            audio = np.asarray(data.get("audio", []), dtype=np.float32).flatten()
            sample_rate = int(data.get("sample_rate", 16000))
            if audio.size == 0:
                self.transcript_ready.emit(request_id, "")
                return

            self.log.emit("ASR", f"[ASR] audio received (length: {audio.size/sample_rate:.2f}s)")
            prep_started_at = time.perf_counter()
            audio = self._resample(audio, sample_rate, 16000)
            prep_elapsed_ms = (time.perf_counter() - prep_started_at) * 1000.0
            decode_started_at = time.perf_counter()
            result = self._model.transcribe(audio, fp16=False)
            decode_elapsed_ms = (time.perf_counter() - decode_started_at) * 1000.0
            text = str(result.get("text", "")).strip()
            total_elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            self.log.emit("ASR", f"[ASR] transcription: {text}")
            self.log.emit(
                "Timing",
                f"ASR transcribe: prep={prep_elapsed_ms:.1f} ms decode={decode_elapsed_ms:.1f} ms total={total_elapsed_ms:.1f} ms",
            )
            self.transcript_ready.emit(request_id, text)
        except Exception as exc:
            self.transcript_failed.emit(request_id, f"Transcription failed: {exc}")

    def _resample(self, audio: object, source_rate: int, target_rate: int) -> object:
        audio_np = np.asarray(audio, dtype=np.float32)
        if source_rate == target_rate or audio_np.size == 0:
            return audio_np

        duration = audio_np.size / float(source_rate)
        target_size = max(1, int(duration * target_rate))
        old_positions = np.linspace(0.0, 1.0, num=audio_np.size, endpoint=False)
        new_positions = np.linspace(0.0, 1.0, num=target_size, endpoint=False)
        return np.interp(new_positions, old_positions, audio_np).astype(np.float32)
