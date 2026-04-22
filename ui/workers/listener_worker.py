from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import QObject, Slot, Signal


class ListenerWorker(QObject):
    devices_ready = Signal(object)
    level_ready = Signal(float)
    speech_started = Signal()
    utterance_ready = Signal(object)
    interrupt_requested = Signal()
    log = Signal(str, str)
    error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._stream = None
        self._active = False
        self._speaking_mode = False
        self._speaking_guard_until = 0.0
        self._threshold = 0.018
        self._silence_seconds = 0.9
        self._max_record_seconds = 12.0
        self._level_fps = 24.0
        self._last_level_emit = 0.0
        self._input_device: int | None = None
        self._sample_rate = 16000
        self._recording = False
        self._buffer: list[Any] = []
        self._silence_samples = 0
        self._voiced_samples = 0
        self._record_started_at = 0.0
        self._interrupt_counter = 0
        self._interrupt_emitted = False

    @Slot()
    def refresh_devices(self) -> None:
        try:
            try:
                import sounddevice as sd
            except Exception as exc:
                self.error.emit(f"Audio input unavailable: {exc}")
                return

            items: list[dict[str, Any]] = []
            try:
                devices = sd.query_devices()
                for index, device in enumerate(devices):
                    if device.get("max_input_channels", 0) < 1:
                        continue
                    hostapi_name = sd.query_hostapis(device["hostapi"])["name"]
                    items.append(
                        {
                            "id": index,
                            "label": f"{device['name']} ({hostapi_name})",
                            "sample_rate": int(device["default_samplerate"]),
                        }
                    )
            except Exception as exc:
                self.error.emit(f"Failed to enumerate input devices: {exc}")
                return

            if self._input_device is None and items:
                self._input_device = items[0]["id"]
            self.devices_ready.emit({"inputs": items, "selected_input": self._input_device})
        except Exception as exc:
            self.error.emit(f"Listener critical error: {exc}")

    @Slot(object)
    def set_input_device(self, device_id: object) -> None:
        try:
            self._input_device = None if device_id in {None, ""} else int(device_id)
            self.log.emit("Listener", f"Input device set to {self._input_device}.")
            if self._stream is not None:
                self.stop_listening()
                self.start_listening()
        except Exception as exc:
            self.error.emit(f"Error setting input device: {exc}")

    @Slot()
    def start_listening(self) -> None:
        try:
            self._active = True
            self._reset_capture()
            self._open_stream()
        except Exception as exc:
            self.error.emit(f"Error starting listener: {exc}")

    @Slot()
    def stop_listening(self) -> None:
        try:
            self._active = False
            self._reset_capture()
            self.level_ready.emit(0.0)
            self._close_stream()
        except Exception as exc:
            self.error.emit(f"Error stopping listener: {exc}")

    @Slot(bool)
    def set_speaking_mode(self, speaking: bool) -> None:
        try:
            self._speaking_mode = speaking
            self._interrupt_counter = 0
            self._interrupt_emitted = False
            self._speaking_guard_until = time.monotonic() + (0.35 if speaking else 0.0)
        except Exception as exc:
            self.error.emit(f"Error setting speaking mode: {exc}")

    def _open_stream(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
        except Exception as exc:
            self.error.emit(f"sounddevice is unavailable: {exc}")
            return

        try:
            if self._input_device is not None:
                info = sd.query_devices(self._input_device)
                self._sample_rate = int(info["default_samplerate"]) or 16000
            else:
                self._sample_rate = 16000

            block_size = max(256, int(self._sample_rate / self._level_fps))
            self._stream = sd.InputStream(
                samplerate=self._sample_rate,
                device=self._input_device,
                channels=1,
                dtype="float32",
                blocksize=block_size,
                callback=self._on_audio,
            )
            self._stream.start()
            self.log.emit("Listener", "Microphone stream started.")
        except Exception as exc:
            self._stream = None
            self.error.emit(f"Unable to start microphone stream: {exc}")

    def _close_stream(self) -> None:
        if self._stream is None:
            return
        try:
            if self._stream.active:
                self._stream.stop()
            self._stream.close()
        except Exception as e:
            self.error.emit(f"Error closing microphone stream: {e}")
        self._stream = None
        self.log.emit("Listener", "Microphone stream stopped.")

    def _on_audio(self, indata, frames, time_info, status) -> None:
        try:
            import numpy as np
            del frames, time_info
            if status:
                self.log.emit("Listener", f"Input stream notice: {status}")

            audio = np.squeeze(np.asarray(indata, dtype=np.float32).copy())
            if audio.ndim == 0:
                audio = np.array([float(audio)], dtype=np.float32)

            energy = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
            level = min(1.0, energy * 10.0)
            now = time.monotonic()
            if now - self._last_level_emit >= 1.0 / self._level_fps:
                self._last_level_emit = now
                self.level_ready.emit(level)

            if not self._active:
                return

            if self._speaking_mode and now >= self._speaking_guard_until and energy > self._threshold * 1.5:
                self._interrupt_counter += 1
                if self._interrupt_counter >= 2 and not self._interrupt_emitted:
                    self._interrupt_emitted = True
                    self.interrupt_requested.emit()
            else:
                self._interrupt_counter = max(0, self._interrupt_counter - 1)

            is_speech = energy > self._threshold
            if is_speech:
                if not self._recording:
                    self._recording = True
                    self._record_started_at = now
                    self._buffer = []
                    self._silence_samples = 0
                    self._voiced_samples = 0
                    self.speech_started.emit()
                self._buffer.append(audio)
                self._voiced_samples += len(audio)
                self._silence_samples = 0
            elif self._recording:
                self._buffer.append(audio)
                self._silence_samples += len(audio)

            if not self._recording:
                return

            if now - self._record_started_at >= self._max_record_seconds:
                self._finish_capture()
                return

            if self._silence_samples >= int(self._sample_rate * self._silence_seconds):
                self._finish_capture()
        except Exception as exc:
            self.error.emit(f"Audio stream error: {exc}")

    def _finish_capture(self) -> None:
        import numpy as np
        audio = np.concatenate(self._buffer).astype(np.float32) if self._buffer else np.array([], dtype=np.float32)
        voiced_seconds = self._voiced_samples / max(1, self._sample_rate)
        self._reset_capture()

        if audio.size == 0 or voiced_seconds < 0.18:
            return
        self.utterance_ready.emit({"audio": audio, "sample_rate": self._sample_rate})

    def _reset_capture(self) -> None:
        self._recording = False
        self._buffer = []
        self._silence_samples = 0
        self._voiced_samples = 0
        self._interrupt_counter = 0
        self._interrupt_emitted = False
