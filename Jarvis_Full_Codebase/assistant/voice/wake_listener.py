import collections
import queue
import time
import warnings
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from assistant.event_bus import Events, bus


logger_name = "Jarvis.WakeListener"

warnings.filterwarnings(
    "ignore",
    message="pkg_resources is deprecated as an API.*",
    category=UserWarning,
)


class WakeListener(QObject):
    wake_detected = Signal()
    speech_started = Signal()
    speech_ended = Signal()
    utterance_ready = Signal(object)
    level_ready = Signal(float)
    devices_ready = Signal(dict)
    log = Signal(str, str)
    error = Signal(str)
    interrupt_requested = Signal()
    audio_captured = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.continuous = False
        self.state = "IDLE"
        self.sample_rate = 16000
        self.chunk_ms = 20
        self.chunk_size = int(self.sample_rate * self.chunk_ms / 1000)
        self.device_id = None
        self._speaking_mode = False
        self._perf_mode = "HIGH"
        self._heartbeat_interval = 2.0
        self._stall_timeout = 3.0
        self._restart_delay = 0.5
        self._next_restart_delay = self._restart_delay
        self._level_emit_interval = 1.0 / 24.0
        self._last_level_emit = 0.0
        self._last_interrupt_time = 0.0
        self._interrupt_debounce = 0.4
        self._last_log_times: dict[str, float] = {}
        self._last_device_message = ""
        self.speech_frames: list[np.ndarray] = []
        self.silence_count = 0
        self.has_spoken = False
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
        self._pre_roll = collections.deque(maxlen=int(1.5 * 1000 / self.chunk_ms))
        self._stream = None
        self._sd_module = None
        self._vad = None
        self._last_chunk_time = 0.0
        self._last_trigger_time = 0.0

        self._poll_timer = QTimer(self)
        self._poll_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._poll_timer.setInterval(max(10, self.chunk_ms // 2))
        self._poll_timer.timeout.connect(self._poll_audio_queue)

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._retry_timer.timeout.connect(self._retry_open_stream)

        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(int(self._heartbeat_interval * 1000))
        self._heartbeat_timer.timeout.connect(self._emit_heartbeat)

        bus.subscribe(Events.PERF_MODE_CHANGED, self._on_perf_mode_changed)

    def _on_perf_mode_changed(self, mode: str) -> None:
        self._perf_mode = mode

    def _emit_log(self, message: str, *, key: str | None = None, min_interval: float = 0.0) -> None:
        now = time.monotonic()
        throttle_key = key or message
        if min_interval > 0.0 and now - self._last_log_times.get(throttle_key, 0.0) < min_interval:
            return
        self._last_log_times[throttle_key] = now
        self.log.emit("WakeListener", message)

    def _emit_heartbeat(self) -> None:
        bus.publish_async("health.heartbeat", "WakeListener")

    def _ensure_audio_backend(self) -> bool:
        try:
            if self._sd_module is None:
                import sounddevice as sd

                self._sd_module = sd
            if self._vad is None:
                import webrtcvad

                self._vad = webrtcvad.Vad(2)
            return True
        except Exception as exc:
            self.error.emit(f"Audio stream error: {exc}")
            return False

    def _reset_capture_state(self) -> None:
        self.speech_frames = []
        self.silence_count = 0
        self.has_spoken = False

    def _drain_queue(self) -> None:
        while True:
            try:
                self._audio_q.get_nowait()
            except queue.Empty:
                break

    def _describe_input_device(self, sd: Any) -> str:
        if self.device_id is not None:
            info = sd.query_devices(self.device_id)
            return f"[WAKE] Using input device {self.device_id}: {info.get('name')} (16000Hz, mono, int16)"
        info = sd.query_devices(kind="input")
        return f"[WAKE] Using default input device: {info.get('name')} (16000Hz, mono, int16)"

    def _should_keep_stream(self) -> bool:
        return self.continuous or self.state == "LISTENING"

    def _finalize_utterance(self) -> None:
        if not self.speech_frames:
            self.state = "IDLE"
            return

        audio_data = np.concatenate(self.speech_frames).astype(np.float32, copy=False)
        self.state = "IDLE"
        self.speech_ended.emit()
        self.utterance_ready.emit({"audio": audio_data, "sample_rate": self.sample_rate})
        self._reset_capture_state()
        self._pre_roll.clear()

        if self.continuous:
            self.state = "IDLE"

    def _process_chunk(self, chunk: np.ndarray, now: float) -> None:
        chunk_int16 = np.asarray(chunk, dtype=np.int16).reshape(-1)
        if chunk_int16.size < self.chunk_size:
            return
        if chunk_int16.size > self.chunk_size:
            chunk_int16 = chunk_int16[:self.chunk_size]

        chunk_float = chunk_int16.astype(np.float32) / 32768.0
        peak = float(np.max(np.abs(chunk_float))) if chunk_float.size else 0.0

        if now - self._last_level_emit >= self._level_emit_interval:
            self._last_level_emit = now
            self.level_ready.emit(peak)

        self.audio_captured.emit(chunk_float)
        is_speech = self._vad.is_speech(chunk_int16.tobytes(), self.sample_rate)

        if self.state == "IDLE":
            self._pre_roll.append(chunk_float)
            if is_speech and (now - self._last_trigger_time >= 0.4):
                if self._speaking_mode and (now - self._last_interrupt_time >= self._interrupt_debounce):
                    self._last_interrupt_time = now
                    self.interrupt_requested.emit()
                self.state = "LISTENING"
                self.speech_started.emit()
                self.speech_frames = list(self._pre_roll)
                self.silence_count = 0
                self.has_spoken = True
                self._last_trigger_time = now
            return

        self.speech_frames.append(chunk_float)
        if is_speech:
            self.silence_count = 0
            self.has_spoken = True
        else:
            self.silence_count += 1

        max_silence = 75 if self.has_spoken else 200
        max_frames = int((30.0 * 1000) / self.chunk_ms)
        if self.silence_count > max_silence or len(self.speech_frames) > max_frames:
            self._finalize_utterance()

    def _open_stream(self) -> None:
        if self._stream is not None or not self._should_keep_stream():
            return
        if not self._ensure_audio_backend():
            return

        sd = self._sd_module
        try:
            device_message = self._describe_input_device(sd)
            if device_message != self._last_device_message:
                self._last_device_message = device_message
                self._emit_log(device_message)

            def callback(indata, frames, time_info, status) -> None:
                del frames, time_info
                if status:
                    self._emit_log(
                        f"[WAKE] Input stream notice: {status}",
                        key="stream_notice",
                        min_interval=5.0,
                    )
                chunk = np.asarray(indata, dtype=np.int16).copy()
                try:
                    self._audio_q.put_nowait(chunk)
                except queue.Full:
                    try:
                        self._audio_q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._audio_q.put_nowait(chunk)
                    except queue.Full:
                        pass

            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.chunk_size,
                device=self.device_id,
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self._schedule_stream_retry(
                f"[WAKE] Stream init failed: {exc}. Retrying in {self._next_restart_delay:.1f}s.",
                key="stream_init_failed",
                min_interval=1.0,
            )
            return

        self._next_restart_delay = self._restart_delay
        self._reset_capture_state()
        self._drain_queue()
        self._last_chunk_time = time.monotonic()
        self._last_trigger_time = 0.0
        self._poll_timer.start()
        self._emit_heartbeat()
        self._emit_log("[WAKE] Stream started. Listening for speech.", key="stream_started", min_interval=2.0)

    def _close_stream(self) -> None:
        self._poll_timer.stop()
        self._retry_timer.stop()
        if self._stream is None:
            return
        try:
            if getattr(self._stream, "active", False):
                self._stream.stop()
            self._stream.close()
        except Exception:
            pass
        self._stream = None

    def _restart_stream(self, message: str, *, key: str, min_interval: float) -> None:
        self._emit_log(message, key=key, min_interval=min_interval)
        self._close_stream()
        if self._should_keep_stream():
            self._retry_timer.start(200)

    def _schedule_stream_retry(self, message: str, *, key: str, min_interval: float) -> None:
        self._emit_log(message, key=key, min_interval=min_interval)
        self._close_stream()
        if not self._should_keep_stream():
            return
        delay_ms = max(100, int(self._next_restart_delay * 1000))
        self._retry_timer.start(delay_ms)
        self._next_restart_delay = min(self._next_restart_delay * 2.0, 2.0)

    @Slot()
    def _retry_open_stream(self) -> None:
        self._open_stream()

    @Slot()
    def _poll_audio_queue(self) -> None:
        if self._stream is None:
            return

        processed = 0
        while True:
            try:
                chunk = self._audio_q.get_nowait()
            except queue.Empty:
                break

            now = time.monotonic()
            self._last_chunk_time = now
            self._process_chunk(chunk, now)
            processed += 1
            if processed >= 16:
                break

        now = time.monotonic()
        if now - self._last_chunk_time >= self._stall_timeout:
            self._restart_stream(
                "[WAKE] Audio stream stalled. Reinitializing input stream.",
                key="stream_stall",
                min_interval=5.0,
            )

    @Slot(bool)
    def set_speaking_mode(self, enabled: bool) -> None:
        self._speaking_mode = enabled
        if not enabled:
            self._last_interrupt_time = 0.0

    @Slot()
    def start_listening_continuous(self) -> None:
        self.continuous = True
        if self.state != "LISTENING":
            self.state = "IDLE"
        if not self._heartbeat_timer.isActive():
            self._heartbeat_timer.start()
        self._emit_heartbeat()
        self._open_stream()

    @Slot()
    def stop_listening_continuous(self) -> None:
        self.continuous = False
        self.stop_listening()
        self._heartbeat_timer.stop()
        self._close_stream()
        self.level_ready.emit(0.0)

    @Slot()
    def start_listening(self) -> None:
        self.state = "LISTENING"
        self._reset_capture_state()
        self._pre_roll.clear()
        if not self._heartbeat_timer.isActive():
            self._heartbeat_timer.start()
        self._emit_heartbeat()
        self._open_stream()
        self.speech_started.emit()
        self._emit_log("[ASR] Manual recording armed.", key="manual_start", min_interval=0.25)

    @Slot()
    def stop_listening(self) -> None:
        if self.state == "LISTENING":
            self.state = "IDLE"
            self._reset_capture_state()
            self._pre_roll.clear()
            self.speech_ended.emit()
        if not self.continuous:
            self._heartbeat_timer.stop()
            self._close_stream()
        self.level_ready.emit(0.0)

    @Slot()
    def refresh_devices(self) -> None:
        try:
            import sounddevice as sd

            inputs = []
            for index, device in enumerate(sd.query_devices()):
                if device["max_input_channels"] > 0:
                    inputs.append({"id": index, "name": device["name"]})
            self.devices_ready.emit({"inputs": inputs, "selected_input": self.device_id})
        except Exception as exc:
            self.error.emit(f"Failed to query devices: {exc}")

    @Slot(object)
    def set_input_device(self, device_id: object) -> None:
        try:
            self.device_id = None if device_id in {"", None} else int(device_id)
            self._emit_log(f"Input device set to {self.device_id}", key="input_device", min_interval=0.25)
            if self._stream is not None:
                self._close_stream()
                self._open_stream()
        except ValueError:
            self.error.emit(f"Invalid input device: {device_id}")

    @Slot()
    def shutdown(self) -> None:
        self.continuous = False
        self.state = "IDLE"
        self._reset_capture_state()
        self._pre_roll.clear()
        self._heartbeat_timer.stop()
        self._close_stream()
        self._drain_queue()
        self.level_ready.emit(0.0)
