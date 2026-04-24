from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from ui.state import AppState, Status
from ui.workers.asr_worker import ASRWorker
from ui.workers.backend_worker import BackendWorker
from jarvis.interfaces.wake_word import route_voice_transcript
from jarvis.voice.wake_listener import WakeListener
from ui.workers.metrics_worker import MetricsWorker
from ui.workers.tts_worker import TTSWorker
from jarvis.global_state import global_state
from jarvis.event_bus import bus, Events
from jarvis.runtime_config import get_audio_config


@dataclass(slots=True)
class _RequestContext:
    origin: str
    text: str


class JarvisBackendBridge(QObject):
    listener_start_requested = Signal()
    listener_stop_requested = Signal()
    listener_continuous_start_requested = Signal()
    listener_continuous_stop_requested = Signal()
    listener_shutdown_requested = Signal()
    listener_speaking_requested = Signal(bool)
    listener_device_requested = Signal(object)
    listener_refresh_requested = Signal()
    asr_load_requested = Signal()
    asr_background_wake_requested = Signal(bool)
    asr_transcribe_requested = Signal(object, int)
    backend_init_requested = Signal()
    backend_process_requested = Signal(int, str)
    backend_cancel_requested = Signal(int)
    tts_init_requested = Signal()
    tts_speak_requested = Signal(str)
    tts_stop_requested = Signal()
    tts_device_requested = Signal(object)
    tts_refresh_requested = Signal()
    metrics_start_requested = Signal()
    metrics_stop_requested = Signal()
    safe_mode_requested = Signal(bool)
    safe_mode_changed = Signal(bool)
    
    wake_detected = Signal()
    welcome_tts_requested = Signal()
    text_response_ready = Signal(str)   # response text for overlay chat

    def __init__(self, state: AppState, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self._started = False
        self._continuous_session = False
        self._wake_listener_enabled = False
        self._request_counter = 0
        self._active_request_id: int | None = None
        self._cancelled_requests: set[int] = set()
        self._pending_transcripts: dict[int, str] = {}
        self._request_contexts: dict[int, _RequestContext] = {}
        self._threads: list[QThread] = []
        self._listener_thread: QThread | None = None
        self._last_activation_time = 0.0
        self._enrollment_mode = False
        self._enrollment_count = 0

        self._listener = WakeListener()
        self._asr = ASRWorker()
        self._backend = BackendWorker()
        self._tts = TTSWorker()
        self._metrics = MetricsWorker()

        self._asr_timer = QTimer(self)
        self._asr_timer.setSingleShot(True)
        self._asr_timer.timeout.connect(self._on_asr_timeout)

        self._pipeline_timer = QTimer(self)
        self._pipeline_timer.setSingleShot(True)
        self._pipeline_timer.timeout.connect(self._on_pipeline_timeout)

        self._setup_threads()
        self._connect_requests()
        self._connect_worker_signals()

        audio_config = get_audio_config()
        self._wake_listener_enabled = bool(audio_config.get("background_wake_enabled", False))
        
        bus.subscribe("state.perf_mode_changed", self._on_perf_mode_changed)
        bus.subscribe("system.safe_mode_engaged", self._on_safe_mode_engaged)

    def _on_safe_mode_engaged(self, reason: str) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._backend, "set_safe_mode", Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True))
        self.safe_mode_changed.emit(True)
        self.state.add_log(f"Safe mode engaged automatically: {reason}", source="System")

    def _setup_threads(self) -> None:
        for name, worker in [
            ("ListenerThread", self._listener),
            ("ASRThread", self._asr),
            ("BackendThread", self._backend),
            ("TTSThread", self._tts),
            ("MetricsThread", self._metrics),
        ]:
            thread = QThread(self)
            thread.setObjectName(name)
            worker.moveToThread(thread)
            thread.finished.connect(worker.deleteLater)
            thread.start()
            self._threads.append(thread)
            if name == "ListenerThread":
                self._listener_thread = thread

    def _connect_requests(self) -> None:
        from PySide6.QtCore import Qt
        qc = Qt.ConnectionType.QueuedConnection

        self.listener_start_requested.connect(self._on_manual_start_requested)
        self.listener_start_requested.connect(self._listener.start_listening, qc)
        self.listener_stop_requested.connect(self._listener.stop_listening, qc)
        self.listener_continuous_start_requested.connect(self._listener.start_listening_continuous, qc)
        self.listener_continuous_stop_requested.connect(self._listener.stop_listening_continuous, qc)
        self.listener_shutdown_requested.connect(self._listener.shutdown, qc)
        self.listener_speaking_requested.connect(self._listener.set_speaking_mode, qc)
        self.listener_device_requested.connect(self._listener.set_input_device, qc)
        self.listener_refresh_requested.connect(self._listener.refresh_devices, qc)

        self.asr_load_requested.connect(self._asr.load_model, qc)
        self.asr_background_wake_requested.connect(self._asr.set_background_wake_enabled, qc)
        self.asr_transcribe_requested.connect(self._asr.transcribe, qc)

        self.backend_init_requested.connect(self._backend.initialize, qc)
        self.backend_process_requested.connect(self._backend.process, qc)
        self.backend_cancel_requested.connect(self._backend.cancel_request_now, qc)

        self.tts_init_requested.connect(self._tts.initialize, qc)
        self.tts_speak_requested.connect(self._tts.speak, qc)
        self.tts_stop_requested.connect(self._tts.stop, qc)
        self.tts_device_requested.connect(self._tts.set_output_device, qc)
        self.tts_refresh_requested.connect(self._tts.refresh_devices, qc)
        self.welcome_tts_requested.connect(self._emit_welcome_tts)

        self.metrics_start_requested.connect(self._metrics.start, qc)
        self.metrics_stop_requested.connect(self._metrics.stop, qc)
        self.safe_mode_requested.connect(self._backend.set_safe_mode, qc)

    @Slot()
    def _emit_welcome_tts(self) -> None:
        self.tts_speak_requested.emit("Jarvis is online and ready.")

    def _connect_worker_signals(self) -> None:
        from PySide6.QtCore import Qt
        qc = Qt.ConnectionType.QueuedConnection
        
        self._listener.devices_ready.connect(self._on_listener_devices_ready, qc)
        self._listener.level_ready.connect(self.state.set_mic_level, qc)
        self._listener.speech_started.connect(self._on_listener_speech_started, qc)
        self._listener.utterance_ready.connect(self._on_listener_utterance_ready, qc)
        self._listener.interrupt_requested.connect(self._on_listener_interrupt, qc)
        self._listener.wake_detected.connect(self.wake_detected, qc)
        self._listener.log.connect(self._on_worker_log, qc)
        self._listener.error.connect(lambda message: self._handle_error("Listener", message), qc)

        self._listener.audio_captured.connect(self._asr.on_audio_captured, qc)
        self._asr.ready.connect(self._on_asr_ready, qc)
        self._asr.transcript_ready.connect(self._on_transcript_ready, qc)
        self._asr.wake_detected.connect(self._on_asr_wake_detected, qc)
        self._asr.transcript_failed.connect(lambda request_id, message: self._handle_request_error(request_id, "ASR", message), qc)
        self._asr.log.connect(self._on_worker_log, qc)

        self._backend.ready.connect(self._on_backend_ready, qc)
        self._backend.request_phase_changed.connect(self._on_backend_request_phase, qc)
        self._backend.stream_update.connect(self._on_backend_stream_update, qc)
        self._backend.response_ready.connect(self._on_backend_response_ready, qc)
        self._backend.request_failed.connect(lambda request_id, message: self._handle_request_error(request_id, "Backend", message), qc)
        self._backend.notifications_ready.connect(self._on_notifications_ready, qc)
        self._backend.runtime_feedback_ready.connect(self._on_runtime_feedback_ready, qc)
        self._backend.log.connect(self._on_worker_log, qc)

        self._tts.ready.connect(self._on_tts_ready, qc)
        self._tts.devices_ready.connect(self._on_tts_devices_ready, qc)
        self._tts.speaking_started.connect(self._on_tts_started, qc)
        self._tts.speaking_finished.connect(self._on_tts_finished, qc)
        self._tts.failed.connect(lambda message: self._handle_error("TTS", message), qc)
        self._tts.log.connect(self._on_worker_log, qc)

        self._metrics.metrics_ready.connect(self.state.set_metrics, qc)

    @Slot()
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.state.add_log("Starting desktop bridge.", source="Bridge")
        self._set_status(Status.IDLE)
        self.state.set_session_running(False)
        self.state.set_model_status("backend", "Loading")
        self.state.set_model_status("asr", "Standby")
        self.state.set_model_status("tts", "Loading")
        self.backend_init_requested.emit()
        # TTS and ASR will be triggered sequentially after backend is ready
        self.metrics_start_requested.emit()
        self.asr_background_wake_requested.emit(self._wake_listener_enabled)
        if self._wake_listener_enabled:
            self.listener_continuous_start_requested.emit()
            self.state.add_log("Background wake listening armed.", source="Bridge")
        else:
            self.state.add_log(
                "Background wake listening is off for stable startup. Use Start or enable it from the tray.",
                source="Bridge",
            )

    @Slot()
    def start_listening(self) -> None:
        if not self.state.backend_online:
            self.state.push_toast("Backend Offline", "Jarvis is still initializing.", "warning")
            return
        if self.state.model_status.get("asr", "") == "Error":
            self.state.push_toast("ASR Error", "Speech model failed to load.", "error")
            return

        self._continuous_session = True
        self._cancel_active_request()
        self.state.set_session_running(True)
        self.state.set_response("")
        self._set_status(Status.LISTENING)
        self.listener_speaking_requested.emit(False)
        self.listener_start_requested.emit()
        self.state.add_log("Listening session armed.", source="Bridge")

    @Slot()
    def stop_listening(self) -> None:
        self._continuous_session = False
        self._cancel_active_request()
        self.listener_speaking_requested.emit(False)
        self.listener_stop_requested.emit()
        if self.state.status == Status.SPEAKING:
            self.tts_stop_requested.emit()
        self.state.set_mic_level(0.0)
        self.state.set_session_running(False)
        self._set_status(Status.IDLE)
        self.state.add_log("Voice session stopped.", source="Bridge")

    @Slot()
    def start_listening_continuous(self) -> None:
        self._wake_listener_enabled = True
        self.asr_background_wake_requested.emit(True)
        self.listener_continuous_start_requested.emit()
        self.state.add_log("Continuous wake-word listening armed.", source="Bridge")
        
    @Slot()
    def stop_listening_continuous(self) -> None:
        self._wake_listener_enabled = False
        self.asr_background_wake_requested.emit(False)
        self.listener_continuous_stop_requested.emit()
        self.state.add_log("Continuous wake-word listening disarmed.", source="Bridge")

    @Slot()
    def interrupt(self) -> None:
        active_request_id = self._active_request_id
        active_context = self._request_contexts.get(active_request_id) if active_request_id is not None else None
        self._cancel_active_request()
        self.listener_speaking_requested.emit(False)
        if self.state.status == Status.SPEAKING:
            self.tts_stop_requested.emit()
        self._set_status(Status.INTERRUPTED)
        self.state.add_log("Manual interrupt requested.", source="Bridge")
        if active_context is not None and active_context.origin == "text":
            QTimer.singleShot(180, lambda: self._set_status(Status.IDLE))
        if self._continuous_session:
            QTimer.singleShot(120, self._resume_listening)

    @Slot()
    def cancel_active(self) -> None:
        if self.state.status == Status.IDLE and self._active_request_id is None:
            return  # Already idle, prevent duplicate cancel
        self._continuous_session = False
        req_id = self._active_request_id
        self._cancel_active_request()
        self.listener_stop_requested.emit()
        self.listener_speaking_requested.emit(False)
        if self.state.status == Status.SPEAKING:
            self.tts_stop_requested.emit()
        self.state.set_mic_level(0.0)
        self.state.set_session_running(False)
        self._set_status(Status.IDLE)
        log_msg = f"[REQ-{req_id}] CANCELLED by user." if req_id else "Active request cancelled."
        self.state.add_log(log_msg, source="Bridge")

    @Slot(str)
    def submit_text(self, text: str) -> None:
        """Submit text directly to the backend (bypasses ASR)."""
        cleaned = text.strip()
        if not cleaned:
            return

        if not self.state.backend_online:
            self.state.push_toast("Backend Offline", "Jarvis is still initializing.", "warning")
            return

        self._continuous_session = False
        self.listener_stop_requested.emit()
        self.listener_speaking_requested.emit(False)
        if self.state.status == Status.SPEAKING:
            self.tts_stop_requested.emit()
        self.state.set_session_running(False)

        self._cancel_active_request()
        self._request_counter += 1
        request_id = self._request_counter
        self._prune_cancelled_requests(request_id)
        self._active_request_id = request_id
        self._cancelled_requests.discard(request_id)
        self._pending_transcripts[request_id] = cleaned
        self._request_contexts[request_id] = _RequestContext(origin="text", text=cleaned)

        self.state.set_transcript(cleaned)
        self.state.set_response("")
        self._set_status(Status.PROCESSING)
        self.state.add_log(f"[REQ-{request_id}] TEXT_SUBMITTED: {cleaned}", source="Chat")
        self.backend_process_requested.emit(request_id, cleaned)

    @Slot()
    def refresh_devices(self) -> None:
        self.listener_refresh_requested.emit()
        self.tts_refresh_requested.emit()

    @Slot(object)
    def set_input_device(self, device_id: object) -> None:
        self.listener_device_requested.emit(device_id)
        self.state.update_devices(selected_input=None if device_id in {None, ""} else int(device_id))

    @Slot(object)
    def set_output_device(self, device_id: object) -> None:
        self.tts_device_requested.emit(device_id)
        self.state.update_devices(selected_output=None if device_id in {None, ""} else int(device_id))

    @Slot()
    def test_sound(self) -> None:
        self.state.add_log("Testing audio output device...", source="Bridge")
        self.tts_speak_requested.emit("Output device routing test successful.")

    @Slot(bool)
    def set_safe_mode(self, enabled: bool) -> None:
        self.safe_mode_requested.emit(enabled)
        self.safe_mode_changed.emit(enabled)
        mode = "ON" if enabled else "OFF"
        self.state.add_log(f"Safe mode toggled {mode}.", source="Bridge")
        self.state.push_toast("Safe Mode", f"Safe mode is now {mode}.", "info")

    @Slot()
    def shutdown(self) -> None:
        self._continuous_session = False
        self._wake_listener_enabled = False
        self.listener_continuous_stop_requested.emit()
        self.listener_stop_requested.emit()
        self.listener_shutdown_requested.emit()
        self.listener_speaking_requested.emit(False)
        if self.state.status == Status.SPEAKING:
            self.tts_stop_requested.emit()
        self.metrics_stop_requested.emit()
        for thread in self._threads:
            thread.quit()
        for thread in self._threads:
            thread.wait(2000)

    def restart_module(self, target: str) -> None:
        """Restart a specific module thread to recover from failure.

        IMPORTANT: All existing signal connections for the target are
        disconnected BEFORE new connections are made.  Without this,
        every restart_module() call adds *another* copy of each connection —
        after 3 restarts every signal fires 4x.
        """
        from PySide6.QtCore import Qt
        qc = Qt.ConnectionType.QueuedConnection

        self.state.add_log(f"Attempting partial recovery for {target}...", source="System")

        def _safe_disconnect(*signals):
            """Disconnect all receivers from each signal, ignoring RuntimeError
            if the signal has no connections."""
            for sig in signals:
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass

        if target == "WakeListener":
            self.listener_continuous_stop_requested.emit()
            self.listener_stop_requested.emit()
            self.listener_shutdown_requested.emit()

            # Disconnect every signal that was connected to the OLD listener.
            _safe_disconnect(
                self.listener_start_requested,
                self.listener_stop_requested,
                self.listener_continuous_start_requested,
                self.listener_continuous_stop_requested,
                self.listener_shutdown_requested,
                self.listener_speaking_requested,
                self.listener_device_requested,
                self.listener_refresh_requested,
            )

            if self._listener_thread is not None:
                self._listener_thread.quit()
                self._listener_thread.wait(2000)
            self._listener = WakeListener()
            thread = QThread(self)
            thread.setObjectName("ListenerThread_Recovered")
            self._listener.moveToThread(thread)
            thread.finished.connect(self._listener.deleteLater)
            thread.start()
            self._threads.append(thread)
            self._listener_thread = thread

            # Re-add the manual-start listener (same-thread, DirectConnection).
            self.listener_start_requested.connect(self._on_manual_start_requested)
            # Reconnect signals to the NEW listener.
            self.listener_start_requested.connect(self._listener.start_listening, qc)
            self.listener_stop_requested.connect(self._listener.stop_listening, qc)
            self.listener_continuous_start_requested.connect(self._listener.start_listening_continuous, qc)
            self.listener_continuous_stop_requested.connect(self._listener.stop_listening_continuous, qc)
            self.listener_shutdown_requested.connect(self._listener.shutdown, qc)
            self.listener_speaking_requested.connect(self._listener.set_speaking_mode, qc)
            self.listener_device_requested.connect(self._listener.set_input_device, qc)
            self.listener_refresh_requested.connect(self._listener.refresh_devices, qc)
            self._listener.devices_ready.connect(self._on_listener_devices_ready, qc)
            self._listener.level_ready.connect(self.state.set_mic_level, qc)
            self._listener.speech_started.connect(self._on_listener_speech_started, qc)
            self._listener.utterance_ready.connect(self._on_listener_utterance_ready, qc)
            self._listener.interrupt_requested.connect(self._on_listener_interrupt, qc)
            self._listener.wake_detected.connect(self.wake_detected, qc)
            self._listener.log.connect(self._on_worker_log, qc)
            self._listener.error.connect(lambda message: self._handle_error("Listener", message), qc)
            self._listener.audio_captured.connect(self._asr.on_audio_captured, qc)

            if self._wake_listener_enabled:
                self.listener_continuous_start_requested.emit()
            if self._continuous_session:
                QTimer.singleShot(120, self._resume_listening)

        elif target == "TTSWorker":
            self.tts_stop_requested.emit()

            # Disconnect TTS signals from old worker.
            _safe_disconnect(
                self.tts_init_requested,
                self.tts_speak_requested,
                self.tts_stop_requested,
                self.tts_device_requested,
                self.tts_refresh_requested,
            )

            self._tts.deleteLater()
            self._tts = TTSWorker()
            thread = QThread(self)
            thread.setObjectName("TTSThread_Recovered")
            self._tts.moveToThread(thread)
            thread.finished.connect(self._tts.deleteLater)
            thread.start()
            self._threads.append(thread)

            self.tts_init_requested.connect(self._tts.initialize, qc)
            self.tts_speak_requested.connect(self._tts.speak, qc)
            self.tts_stop_requested.connect(self._tts.stop, qc)
            self.tts_device_requested.connect(self._tts.set_output_device, qc)
            self.tts_refresh_requested.connect(self._tts.refresh_devices, qc)
            self._tts.ready.connect(self._on_tts_ready, qc)
            self._tts.devices_ready.connect(self._on_tts_devices_ready, qc)
            self._tts.speaking_started.connect(self._on_tts_started, qc)
            self._tts.speaking_finished.connect(self._on_tts_finished, qc)
            self._tts.failed.connect(lambda message: self._handle_error("TTS", message), qc)
            self._tts.log.connect(self._on_worker_log, qc)
            self.tts_init_requested.emit()

        elif target == "ASRWorker":
            # Disconnect ASR signals from old worker.
            _safe_disconnect(
                self.asr_load_requested,
                self.asr_background_wake_requested,
                self.asr_transcribe_requested,
            )

            self._asr.deleteLater()
            self._asr = ASRWorker()
            thread = QThread(self)
            thread.setObjectName("ASRThread_Recovered")
            self._asr.moveToThread(thread)
            thread.finished.connect(self._asr.deleteLater)
            thread.start()
            self._threads.append(thread)

            self.asr_load_requested.connect(self._asr.load_model, qc)
            self.asr_background_wake_requested.connect(self._asr.set_background_wake_enabled, qc)
            self.asr_transcribe_requested.connect(self._asr.transcribe, qc)
            self._asr.ready.connect(self._on_asr_ready, qc)
            self._asr.transcript_ready.connect(self._on_transcript_ready, qc)
            self._asr.transcript_failed.connect(lambda request_id, message: self._handle_request_error(request_id, "ASR", message), qc)
            self._asr.log.connect(self._on_worker_log, qc)
            self.asr_background_wake_requested.emit(self._wake_listener_enabled)
            self.asr_load_requested.emit()
        else:
            self.state.add_log(f"Cannot partially recover unknown target: {target}", source="System")

    @Slot()
    def _on_manual_start_requested(self) -> None:
        import time
        self._last_activation_time = time.time()
        self.state.add_log("[ASR WAKE] manual activation", source="UI")

    @Slot()
    def _on_asr_timeout(self) -> None:
        if self.state.status == Status.RECOGNIZING:
            self.state.add_log("[FAILSAFE] ASR timed out (>30s). Restarting ASRWorker.", source="System")
            self._cancel_active_request()
            self.restart_module("ASRWorker")
            self._set_status(Status.IDLE)
            if self._continuous_session:
                QTimer.singleShot(500, self._resume_listening)

    @Slot()
    def _on_pipeline_timeout(self) -> None:
        if self.state.status in {Status.RECOGNIZING, Status.THINKING, Status.PROCESSING, Status.EXECUTING}:
            self.state.add_log("[FAILSAFE] Pipeline timed out (>120s). Resetting system.", source="System")
            if self._active_request_id is not None:
                context = self._request_contexts.get(self._active_request_id)
                if context is not None and context.origin == "text":
                    self._handle_request_error(self._active_request_id, "Backend", "The request timed out.")
                    return
            self._cancel_active_request()
            self._set_status(Status.IDLE)
            if self._continuous_session:
                QTimer.singleShot(500, self._resume_listening)

    def _cancel_active_request(self) -> None:
        request_id = self._active_request_id
        if request_id is None:
            return
        self._cancelled_requests.add(request_id)
        self._pending_transcripts.pop(request_id, None)
        self._request_contexts.pop(request_id, None)
        self.backend_cancel_requested.emit(request_id)
        self.state.add_log(f"[REQ-{request_id}] BACKEND_CANCELLED", source="Bridge")
        self._active_request_id = None

    def _prune_cancelled_requests(self, request_id: int) -> None:
        if len(self._cancelled_requests) <= 50:
            return
        cutoff = request_id - 50
        self._cancelled_requests = {
            rid for rid in self._cancelled_requests if rid >= cutoff
        }

    def _resume_listening(self) -> None:
        if not self._continuous_session:
            return
        if self.state.status in {Status.RECOGNIZING, Status.THINKING}:
            return
        self.listener_speaking_requested.emit(False)
        self.listener_start_requested.emit()
        self._set_status(Status.LISTENING)

    def _set_status(self, status: str) -> None:
        self.state.set_status(status)
        global_state.set_status(status)
        self.state.set_can_interrupt(
            status in {
                Status.RECOGNIZING,
                Status.THINKING,
                Status.PROCESSING,
                Status.EXECUTING,
                Status.SPEAKING,
            }
        )
        
        # Manage failsafe timers (main thread only)
        if status == Status.RECOGNIZING:
            self._asr_timer.start(30000)
            self._pipeline_timer.start(120000)
        elif status in {Status.THINKING, Status.PROCESSING, Status.EXECUTING}:
            if self._asr_timer.isActive():
                self._asr_timer.stop()
            self._pipeline_timer.start(120000)
        else:
            if self._asr_timer.isActive():
                self._asr_timer.stop()
            if self._pipeline_timer.isActive():
                self._pipeline_timer.stop()

    def _on_perf_mode_changed(self, mode: str) -> None:
        # Fired by PerformanceManager via EventBus
        low_perf = (mode == "LOW")
        self.state.set_perf_mode(low_perf)
        # In a real Qt setup, we'd emit a signal. For now, we will add a log.
        self.state.add_log(f"Performance mode changed to {mode}", source="System")

    def _on_worker_log(self, source: str, message: str) -> None:
        self.state.add_log(message, source=source)

    def _on_listener_devices_ready(self, payload: dict[str, Any]) -> None:
        self.state.update_devices(
            inputs=payload.get("inputs", []),
            selected_input=payload.get("selected_input"),
        )

    def _on_tts_devices_ready(self, payload: dict[str, Any]) -> None:
        self.state.update_devices(
            outputs=payload.get("outputs", []),
            selected_output=payload.get("selected_output"),
        )

    def _on_asr_ready(self, payload: dict[str, Any]) -> None:
        status = payload.get("status", "Unknown")
        self.state.set_model_status("asr", status)
        if status != "Ready":
            error = payload.get("error", "Whisper could not be loaded.")
            self._handle_error("ASR", str(error))

    def _on_backend_ready(self, payload: dict[str, Any]) -> None:
        status = payload.get("status", "Unknown")
        self.state.set_model_status("backend", status)
        online = status == "Ready"
        self.safe_mode_changed.emit(bool(payload.get("safe_mode", False)))
        self.state.set_backend_online(online)
        if not online:
            self._handle_error("Backend", str(payload.get("error", "Backend unavailable.")))
        else:
            self.state.add_log("Backend is online. Starting TTS...", source="Bridge")
            self.tts_init_requested.emit()
            self.tts_refresh_requested.emit()

    def _on_tts_ready(self, payload: dict[str, Any]) -> None:
        status = payload.get("status", "Unknown")
        self.state.set_model_status("tts", status)
        if status == "Error":
            self._handle_error("TTS", str(payload.get("error", "TTS unavailable.")))
        
        self.state.add_log(f"TTS ready ({status}). Starting ASR...", source="Bridge")
        self.asr_load_requested.emit()
        self.listener_refresh_requested.emit()

    def _on_listener_speech_started(self) -> None:
        if self._continuous_session:
            self._set_status(Status.LISTENING)

    def _on_listener_interrupt(self) -> None:
        if self.state.status != Status.SPEAKING:
            return
        self.state.add_log("Voice interrupt detected during playback.", source="Listener")
        self._cancel_active_request()
        self.listener_speaking_requested.emit(False)
        self.tts_stop_requested.emit()
        self._set_status(Status.INTERRUPTED)
        # Transition to LISTENING quickly so the user's speech is captured
        if self._continuous_session:
            QTimer.singleShot(50, self._resume_listening)

    def _on_listener_utterance_ready(self, payload: object) -> None:
        if not self._continuous_session:
            return

        payload_data = payload if isinstance(payload, dict) else {"audio": payload, "sample_rate": 16000}

        # ── Voice enrollment mode ────────────────────────────────────
        if self._enrollment_mode:
            try:
                import numpy as np
                from jarvis.voice_identity import voice_id
                audio = np.asarray(payload_data.get("audio", []), dtype=np.float32)
                done = voice_id.add_enrollment_sample(audio, sample_rate=16000)
                self._enrollment_count += 1
                remaining = 3 - self._enrollment_count
                if done:
                    self._enrollment_mode = False
                    self._enrollment_count = 0
                    self.state.push_toast(
                        "Voice Enrolled",
                        "Your voice profile has been saved. Jarvis will now recognize you.",
                        "info",
                    )
                    self.state.add_log("Voice enrollment complete.", source="VoiceID")
                    from jarvis.event_bus import bus as event_bus
                    from jarvis.event_bus import Events
                    event_bus.publish_async(Events.VOICE_ENROLLED, {})
                else:
                    self.state.push_toast(
                        "Voice Enrollment",
                        f"Sample {self._enrollment_count}/3 recorded. {remaining} more needed.",
                        "info",
                    )
            except Exception as e:
                self.state.push_toast("Enrollment Error", str(e), "error")
                self._enrollment_mode = False
            return

        self.listener_stop_requested.emit()
        self.listener_speaking_requested.emit(False)
        self._set_status(Status.RECOGNIZING)
        self._request_counter += 1
        request_id = self._request_counter
        self._prune_cancelled_requests(request_id)
        self._active_request_id = request_id
        self._cancelled_requests.discard(request_id)
        self._request_contexts[request_id] = _RequestContext(origin="voice", text="")
        self.state.add_log(f"[REQ-{request_id}] LISTENING_DONE", source="Listener")
        self.asr_transcribe_requested.emit(payload_data, request_id)

    @Slot(str)
    def _on_asr_wake_detected(self, command: str) -> None:
        import time
        now = time.time()
        # Debounce
        if now - self._last_activation_time < 2.0:
            return
            
        self._last_activation_time = now
        self.state.add_log("[ASR WAKE] detected wake phrase", source="ASR")
        
        if command:
            self.state.add_log(f"[ASR WAKE] command extracted: {command}", source="ASR")
            self._request_counter += 1
            request_id = self._request_counter
            self._prune_cancelled_requests(request_id)
            self._active_request_id = request_id
            self._request_contexts[request_id] = _RequestContext(origin="voice", text=command)
            self.state.set_transcript(command)
            self._set_status(Status.THINKING)
            self.backend_process_requested.emit(request_id, command)
            # Force listener back to idle since we handled it
            self.listener_stop_requested.emit()
        else:
            # Just "Jarvis" - stay in listening to catch the rest
            self._set_status(Status.LISTENING)

    def _on_transcript_ready(self, request_id: int, transcript: str) -> None:
        if request_id in self._cancelled_requests or request_id != self._active_request_id:
            return

        text = transcript.strip()
        if not text:
            self._active_request_id = None
            self._resume_listening()
            return

        import time
        now = time.time()
        wake_decision = route_voice_transcript(
            text,
            in_wake_window=now - self._last_activation_time <= 10.0,
        )
        if not wake_decision.accepted:
            self._active_request_id = None
            self._resume_listening()
            return
        if wake_decision.activated or wake_decision.keep_listening:
            self._last_activation_time = now
        elif now - self._last_activation_time <= 10.0:
            self._last_activation_time = now
        text = wake_decision.command

        self._pending_transcripts[request_id] = text
        self._request_contexts[request_id] = _RequestContext(origin="voice", text=text)
        self.state.set_transcript(text)
        self.state.set_response("")
        self._set_status(Status.THINKING)
        self.state.add_log(f"[REQ-{request_id}] BACKEND_SENT: {text}", source="Bridge")
        self.backend_process_requested.emit(request_id, text)

    def _on_backend_request_phase(self, request_id: int, phase: str, payload: object) -> None:
        del payload
        if request_id in self._cancelled_requests or request_id != self._active_request_id:
            return

        upper = (phase or "").upper()
        status_map = {
            "THINKING": Status.THINKING,
            "PROCESSING": Status.PROCESSING,
            "EXECUTING": Status.EXECUTING,
            "RESPONDING": Status.RESPONDING,
            "CANCELLED": Status.INTERRUPTED,
            "ERROR": Status.ERROR,
        }
        mapped = status_map.get(upper)
        if mapped is not None:
            self._set_status(mapped)

    def _on_backend_stream_update(self, request_id: int, streamed_text: str, payload: object) -> None:
        if request_id in self._cancelled_requests or request_id != self._active_request_id:
            return

        text = (streamed_text or "").strip()
        if not text:
            return

        self.state.set_response(text)
        details = payload if isinstance(payload, dict) else {}
        if details.get("final"):
            self.state.add_log("Execution stream finished.", source="Backend")
        else:
            self.state.add_log(text.splitlines()[-1], source="Backend")
        self.text_response_ready.emit(text)

    def _on_runtime_feedback_ready(self, payload: object) -> None:
        details = payload if isinstance(payload, dict) else {}
        payload_type = str(details.get("type", "")).strip().lower()
        if payload_type == "activity":
            activity = str(details.get("activity", "")).strip()
            if activity:
                self.state.set_safety_feedback(activity=activity)
        elif payload_type == "safety":
            self.state.set_safety_feedback(
                safety_level=str(details.get("safety_level", "SAFE")).strip() or "SAFE",
                reason=str(details.get("reason", "")).strip(),
                activity=str(details.get("activity", "")).strip(),
                scope=str(details.get("scope", "single")).strip() or "single",
            )
        elif payload_type == "finalized":
            self.state.set_safety_feedback(
                activity=str(details.get("activity", "Idle")).strip() or "Idle",
                reason=str(details.get("reason", "")).strip() or "Awaiting validated work.",
            )

    def _on_backend_response_ready(self, request_id: int, response: str, snapshot: object) -> None:
        if request_id in self._cancelled_requests or request_id != self._active_request_id:
            return

        context = self._request_contexts.pop(request_id, None)
        user_text = self._pending_transcripts.pop(
            request_id,
            context.text if context is not None else self.state.transcript,
        )
        final_response = (response or "").strip()
        if not final_response:
            final_response = "The backend finished without returning a response."

        self.state.append_turn(user_text, final_response)
        self.state.set_response(final_response)
        self.state.add_log(f"[REQ-{request_id}] BACKEND_RESPONSE", source="Backend")
        self.text_response_ready.emit(final_response)

        if context is not None and context.origin == "text":
            self._set_status(Status.RESPONDING)
            self._active_request_id = None
            QTimer.singleShot(450, self._finish_text_cycle)
            return

        self._set_status(Status.SPEAKING)
        if self._continuous_session:
            self.listener_start_requested.emit()
            
        if self.state.model_status.get("tts") == "Ready":
            self.tts_speak_requested.emit(final_response)
        else:
            self.state.add_log("TTS engine unavailable, skipping speech.", source="TTS")
            self._on_tts_finished(False)

    def _finish_text_cycle(self) -> None:
        if self.state.status in {Status.RESPONDING, Status.ERROR}:
            self._set_status(Status.IDLE)

    def _on_notifications_ready(self, notifications: list[str]) -> None:
        for note in notifications:
            self.state.push_toast("Reminder", note, "info")
            self.state.add_log(note, source="Scheduler")

    def _on_tts_started(self, text: str) -> None:
        if self._continuous_session:
            self.listener_speaking_requested.emit(True)
        if self._active_request_id is not None:
            self.state.add_log(f"[REQ-{self._active_request_id}] TTS_START", source="Bridge")
        preview = text if len(text) < 80 else text[:77] + "..."
        self.state.add_log(f"Speaking: {preview}", source="TTS")

    def _on_tts_finished(self, interrupted: bool) -> None:
        if self._active_request_id is not None:
            self.state.add_log(f"[REQ-{self._active_request_id}] TTS_DONE", source="Bridge")
            self._active_request_id = None
        
        self.state.add_log(f"TTS playback finished (interrupted={interrupted}).", source="Bridge")
        self._active_request_id = None
        self.listener_speaking_requested.emit(False)
        if self.state.status in {Status.RECOGNIZING, Status.THINKING, Status.PROCESSING, Status.EXECUTING}:
            return
        if interrupted:
            self.state.add_log("Speech playback interrupted.", source="TTS")
        if self._continuous_session:
            self._resume_listening()
        else:
            self._set_status(Status.IDLE)

    def _handle_request_error(self, request_id: int, source: str, message: str) -> None:
        if request_id in self._cancelled_requests:
            self._cancelled_requests.discard(request_id)
            return
        self._cancelled_requests.discard(request_id)
        context = self._request_contexts.pop(request_id, None)
        user_text = self._pending_transcripts.pop(
            request_id,
            context.text if context is not None else self.state.transcript,
        )
        self._active_request_id = None

        if context is not None and context.origin == "text":
            final_message = message.strip() or f"{source} could not complete the request."
            self.state.append_turn(user_text, final_message)
            self.state.set_response(final_message)
            self.text_response_ready.emit(final_message)
            self.state.push_error(final_message, source=source)
            self.state.push_toast(f"{source} Error", final_message, "error")
            self._set_status(Status.ERROR)
            QTimer.singleShot(1400, self._finish_text_cycle)
            return

        self._handle_error(source, message)

    def _handle_error(self, source: str, message: str) -> None:
        self.state.push_error(message, source=source)
        self.state.push_toast(f"{source} Error", message, "error")
        self._set_status(Status.ERROR)
        if self._continuous_session:
            QTimer.singleShot(1600, self._resume_listening)

    @property
    def wake_listener_enabled(self) -> bool:
        return self._wake_listener_enabled
