from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ui.state import AppState, Status
from ui.widgets.mic_orb import MicOrbWidget
from ui.widgets.status_badge import StatusBadge


def _tone_for_runtime(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in {"ready", "online"}):
        return "success"
    if any(word in lowered for word in {"error", "failed", "offline"}):
        return "danger"
    if any(word in lowered for word in {"loading", "warming"}):
        return "warning"
    return "muted"


class ControlsPanel(QFrame):
    start_requested = Signal()
    stop_requested = Signal()
    interrupt_requested = Signal()

    def __init__(self, state: AppState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self.setObjectName("panel")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 22)
        root.setSpacing(18)

        eyebrow = QLabel("VOICE CONTROL")
        eyebrow.setObjectName("sectionEyebrow")
        title = QLabel("Hands-free command console")
        title.setObjectName("panelTitle")

        self._status_badge = StatusBadge(Status.IDLE, "muted")
        self._backend_badge = StatusBadge("Backend Offline", "danger")
        self._asr_badge = StatusBadge("Whisper Loading", "warning")
        self._tts_badge = StatusBadge("TTS Loading", "warning")
        self._safety_badge = StatusBadge("SAFE", "success")
        self._backend_online = state.backend_online
        self._asr_ready = state.model_status.get("asr") == "Ready"

        badge_row = QHBoxLayout()
        badge_row.setSpacing(8)
        badge_row.addWidget(self._status_badge)
        badge_row.addWidget(self._backend_badge)
        badge_row.addWidget(self._safety_badge)

        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        model_row.addWidget(self._asr_badge)
        model_row.addWidget(self._tts_badge)

        self._orb = MicOrbWidget()
        self._orb.set_status(Status.IDLE)

        self._hint = QLabel("Start a session to begin live listening. The orb responds to real microphone input only.")
        self._hint.setObjectName("mutedText")
        self._hint.setWordWrap(True)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._safety_hint = QLabel("Activity: Idle\nValidation: Awaiting validated work.")
        self._safety_hint.setObjectName("mutedText")
        self._safety_hint.setWordWrap(True)
        self._safety_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)

        self._start_button = QPushButton("Start")
        self._start_button.setObjectName("primaryButton")
        self._start_button.clicked.connect(self.start_requested.emit)

        self._stop_button = QPushButton("Stop")
        self._stop_button.setObjectName("secondaryButton")
        self._stop_button.clicked.connect(self.stop_requested.emit)

        self._interrupt_button = QPushButton("Interrupt")
        self._interrupt_button.setObjectName("dangerButton")
        self._interrupt_button.clicked.connect(self.interrupt_requested.emit)

        button_row.addWidget(self._start_button)
        button_row.addWidget(self._stop_button)
        button_row.addWidget(self._interrupt_button)

        root.addWidget(eyebrow)
        root.addWidget(title)
        root.addLayout(badge_row)
        root.addLayout(model_row)
        root.addStretch()
        root.addWidget(self._orb, 0, Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._hint)
        root.addWidget(self._safety_hint)
        root.addStretch()
        root.addLayout(button_row)

        state.status_changed.connect(self._on_status_changed)
        state.mic_level_changed.connect(self._orb.set_level)
        state.backend_online_changed.connect(self._on_backend_online_changed)
        state.model_status_changed.connect(self._on_model_status_changed)
        state.session_running_changed.connect(self._on_session_running_changed)
        state.interrupt_capability_changed.connect(self._on_interrupt_capability_changed)
        state.safety_feedback_changed.connect(self._on_safety_feedback_changed)

        self._on_status_changed(state.status)
        self._on_backend_online_changed(state.backend_online)
        self._on_model_status_changed(state.model_status)
        self._on_session_running_changed(state.session_running)
        self._on_interrupt_capability_changed(state.can_interrupt)
        self._on_safety_feedback_changed(state.safety_feedback)

    def _on_status_changed(self, status: str) -> None:
        tone_map = {
            Status.IDLE: "muted",
            Status.TYPING: "muted",
            Status.LISTENING: "success",
            Status.RECOGNIZING: "warning",
            Status.THINKING: "warning",
            Status.PROCESSING: "warning",
            Status.EXECUTING: "accent",
            Status.RESPONDING: "accent",
            Status.SPEAKING: "accent",
            Status.INTERRUPTED: "warning",
            Status.ERROR: "danger",
        }
        self._status_badge.set_text(status, tone_map.get(status, "muted"))
        self._orb.set_status(status)

    def _on_backend_online_changed(self, online: bool) -> None:
        self._backend_online = online
        self._backend_badge.set_text("Backend Online" if online else "Backend Offline", "success" if online else "danger")
        self._update_start_button()

    def _on_model_status_changed(self, model_status: dict[str, str]) -> None:
        asr_status = model_status.get("asr", "Unknown")
        tts_status = model_status.get("tts", "Unknown")
        self._asr_badge.set_text(f"Whisper {asr_status}", _tone_for_runtime(asr_status))
        self._tts_badge.set_text(f"TTS {tts_status}", _tone_for_runtime(tts_status))
        self._asr_ready = asr_status == "Ready"
        self._update_start_button()

    def _on_session_running_changed(self, running: bool) -> None:
        self._update_start_button()
        self._stop_button.setEnabled(running)

    def _on_interrupt_capability_changed(self, enabled: bool) -> None:
        self._interrupt_button.setEnabled(enabled)

    def _on_safety_feedback_changed(self, payload: dict[str, str]) -> None:
        safety_level = payload.get("safety_level", "SAFE")
        tone = {
            "SAFE": "success",
            "CONFIRMATION REQUIRED": "warning",
            "BLOCKED": "danger",
        }.get(safety_level, "muted")
        self._safety_badge.set_text(safety_level, tone)
        activity = payload.get("activity", "Idle") or "Idle"
        reason = payload.get("reason", "Awaiting validated work.") or "Awaiting validated work."
        self._safety_hint.setText(f"Activity: {activity}\nValidation: {reason}")

    def _update_start_button(self) -> None:
        ready = self._backend_online and self._asr_ready
        self._start_button.setEnabled(ready and not self._state.session_running)
        if self._backend_online and not self._asr_ready:
            self._start_button.setToolTip("Waiting for Whisper model to load...")
        else:
            self._start_button.setToolTip("")
