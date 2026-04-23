from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger("Jarvis.TTSWorker")


class TTSWorker(QObject):
    ready = Signal(object)
    devices_ready = Signal(object)
    speaking_started = Signal(str)
    speaking_finished = Signal(bool)
    failed = Signal(str)
    log = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._engine = None
        self._output_device: int | None = None
        self._current_text: str = ""

    @Slot()
    def initialize(self) -> None:
        try:
            if self._engine is not None:
                status = "Ready" if getattr(self._engine, "enabled", False) else "Unavailable"
                self.ready.emit({"status": status})
                return

            from jarvis.voice.tts import TTS

            self._engine = TTS()
            self._engine.set_on_finished(self._on_finished)
            self._engine.set_on_started(self._on_started)
            if self._output_device is not None:
                self._engine.output_device = self._output_device

            status = "Ready" if getattr(self._engine, "enabled", False) else "Unavailable"
            self.ready.emit({"status": status})
            self.log.emit("TTS", f"TTS engine status: {status}.")

            # Refresh devices after notifying ready so the init chain can continue.
            self.refresh_devices()
        except Exception as exc:
            self._engine = None
            logger.exception("TTS worker initialization failed")
            self.ready.emit({"status": "Error", "error": f"TTS Init error: {exc}"})

    @Slot()
    def refresh_devices(self) -> None:
        try:
            try:
                import sounddevice as sd
            except Exception as exc:
                self.failed.emit(f"Audio output unavailable: {exc}")
                return

            items: list[dict[str, Any]] = []
            try:
                devices = sd.query_devices()
                for index, device in enumerate(devices):
                    if device.get("max_output_channels", 0) < 1:
                        continue
                    hostapi_name = sd.query_hostapis(device["hostapi"])["name"]
                    items.append({"id": index, "label": f"{device['name']} ({hostapi_name})"})
            except Exception as exc:
                self.failed.emit(f"Failed to enumerate output devices: {exc}")
                return

            self.devices_ready.emit({"outputs": items, "selected_output": self._output_device})
        except Exception as exc:
            self.failed.emit(f"Error refreshing devices: {exc}")

    @Slot(object)
    def set_output_device(self, device_id: object) -> None:
        try:
            self._output_device = None if device_id in {None, ""} else int(device_id)
            if self._engine is not None:
                self._engine.output_device = self._output_device
            self.log.emit("TTS", f"Output device set to {self._output_device}.")
        except Exception as exc:
            self.failed.emit(f"Error setting output device: {exc}")

    @Slot(str)
    def speak(self, text: str) -> None:
        print(f"[TTS] Request received")
        try:
            if not text.strip():
                self.speaking_finished.emit(False)
                return
            if self._engine is None:
                self.initialize()
            if self._engine is None:
                self.failed.emit("TTS engine could not be initialized.")
                self.speaking_finished.emit(True)
                return

            self._current_text = text
            self.log.emit("TTS", f"[TTS] generating: {text}")
            try:
                if self._output_device is not None:
                    self._engine.output_device = self._output_device
                self._engine.speak(text, blocking=False)
            except Exception as exc:
                self.failed.emit(f"TTS failed: {exc}")
                self.speaking_finished.emit(True)
        except Exception as exc:
            self.failed.emit(f"Speak error: {exc}")
            self.speaking_finished.emit(True)

    @Slot()
    def stop(self) -> None:
        try:
            if self._engine is None:
                self.speaking_finished.emit(True)
                return
            try:
                self._engine.stop()
            except Exception as exc:
                self.failed.emit(f"Failed to stop TTS: {exc}")
                self.speaking_finished.emit(True)
        except Exception as exc:
            self.failed.emit(f"Stop error: {exc}")
            self.speaking_finished.emit(True)

    def _on_started(self) -> None:
        self.log.emit("TTS", f"[TTS] playing")
        self.speaking_started.emit(self._current_text)

    def _on_finished(self, interrupted: bool = False) -> None:
        self.speaking_finished.emit(bool(interrupted))
