"""Python ↔ JS bridge exposed via QWebChannel inside QWebEngineView."""
from __future__ import annotations
from PySide6.QtCore import QObject, Signal, Slot
from ui.state import AppState, Status


class WebBridge(QObject):
    """Exposed to JavaScript as `bridge`. Relays state signals to JS callbacks."""

    # Signals JS listens to (connected via channel.objects.bridge.<signal>.connect)
    logAdded = Signal(str, str, str, str)       # timestamp, level, source, message
    statusChanged = Signal(str)
    transcriptChanged = Signal(str)
    responseChanged = Signal(str)
    backendOnlineChanged = Signal(bool)
    modelStatusChanged = Signal(str, str, str)  # backend, asr, tts
    metricsChanged = Signal(float, float)       # cpu, ram
    micLevelChanged = Signal(float)
    sessionRunningChanged = Signal(bool)
    chatMessage = Signal(str, str)              # role("user"|"jarvis"), text

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self._bridge_ref = None  # set after bootstrap

        # Wire AppState signals
        state.log_added.connect(self._on_log)
        state.status_changed.connect(self.statusChanged.emit)
        state.transcript_changed.connect(self.transcriptChanged.emit)
        state.response_changed.connect(self._on_response)
        state.backend_online_changed.connect(self.backendOnlineChanged.emit)
        state.model_status_changed.connect(self._on_model_status)
        state.metrics_changed.connect(self._on_metrics)
        state.mic_level_changed.connect(self.micLevelChanged.emit)
        state.session_running_changed.connect(self.sessionRunningChanged.emit)
        state.history_changed.connect(self._on_history)

    def set_backend_bridge(self, bridge):
        self._bridge_ref = bridge

    # --- Slots callable from JavaScript ---

    @Slot(str)
    def submitText(self, text: str):
        if self._bridge_ref:
            self.chatMessage.emit("user", text.strip())
            self._bridge_ref.submit_text(text)

    @Slot()
    def startListening(self):
        if self._bridge_ref:
            self._bridge_ref.start_listening()

    @Slot()
    def stopListening(self):
        if self._bridge_ref:
            self._bridge_ref.stop_listening()

    @Slot()
    def interruptAction(self):
        if self._bridge_ref:
            self._bridge_ref.interrupt()

    @Slot(result=str)
    def getStatus(self):
        return self.state.status

    @Slot(result=bool)
    def isBackendOnline(self):
        return self.state.backend_online

    @Slot(result=str)
    def getBackendStatus(self):
        return self.state.model_status.get("backend", "Offline")

    @Slot(result=str)
    def getAsrStatus(self):
        return self.state.model_status.get("asr", "Loading")

    @Slot(result=str)
    def getTtsStatus(self):
        return self.state.model_status.get("tts", "Loading")

    # --- Internal handlers ---

    def _on_log(self, entry_dict):
        d = entry_dict if isinstance(entry_dict, dict) else {}
        self.logAdded.emit(
            d.get("timestamp", ""),
            d.get("level", "INFO"),
            d.get("source", ""),
            d.get("message", ""),
        )

    def _on_response(self, text: str):
        if text.strip():
            self.chatMessage.emit("jarvis", text.strip())

    def _on_model_status(self, ms: dict):
        self.modelStatusChanged.emit(
            ms.get("backend", "Offline"),
            ms.get("asr", "Loading"),
            ms.get("tts", "Loading"),
        )

    def _on_metrics(self, m: dict):
        self.metricsChanged.emit(
            float(m.get("cpu", 0)),
            float(m.get("ram", 0)),
        )

    def _on_history(self):
        pass  # History is built from individual chatMessage signals
