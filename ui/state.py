from __future__ import annotations

from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
import logging
from typing import Any

from PySide6.QtCore import QObject, QMutex, QMutexLocker, Signal, Slot


class Status:
    IDLE = "Idle"
    TYPING = "Typing"
    LISTENING = "Listening"
    RECOGNIZING = "Recognizing"
    THINKING = "Thinking"
    PROCESSING = "Processing"
    EXECUTING = "Executing"
    RESPONDING = "Responding"
    SPEAKING = "Speaking"
    INTERRUPTED = "Interrupted"
    ERROR = "Error"


@dataclass(slots=True)
class LogEntry:
    timestamp: str
    level: str
    source: str
    message: str


class AppState(QObject):
    status_changed = Signal(str)
    transcript_changed = Signal(str)
    response_changed = Signal(str)
    mic_level_changed = Signal(float)
    log_added = Signal(object)
    logs_changed = Signal()
    metrics_changed = Signal(dict)
    backend_online_changed = Signal(bool)
    model_status_changed = Signal(dict)
    errors_changed = Signal()
    devices_changed = Signal(dict)
    history_changed = Signal()
    session_running_changed = Signal(bool)
    interrupt_capability_changed = Signal(bool)
    toast_requested = Signal(str, str, str)
    perf_mode_changed = Signal(bool)
    safety_feedback_changed = Signal(dict)

    _instance = None
    _mutex = QMutex()

    @classmethod
    def get_instance(cls) -> "AppState":
        with QMutexLocker(cls._mutex):
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        super().__init__()
        self.status = Status.IDLE
        self.transcript = ""
        self.response = ""
        self.mic_level = 0.0
        self.low_perf = False
        self.logs: deque[LogEntry] = deque(maxlen=500)
        self.metrics = {"cpu": 0.0, "ram": 0.0, "gpu": None}
        self.backend_online = False
        self.errors: deque[dict[str, str]] = deque(maxlen=50)
        self.session_running = False
        self.can_interrupt = False
        self.model_status = {
            "backend": "Offline",
            "asr": "Loading",
            "tts": "Loading",
        }
        self.devices = {
            "inputs": [],
            "outputs": [],
            "selected_input": None,
            "selected_output": None,
        }
        self.history: deque[dict[str, str]] = deque(maxlen=40)
        self.safety_feedback = {
            "safety_level": "SAFE",
            "reason": "Awaiting validated work.",
            "activity": "Idle",
            "scope": "single",
        }

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().strftime("%H:%M:%S")

    @Slot(str)
    def set_status(self, status: str) -> None:
        if self.status == status:
            return
        self.status = status
        self.status_changed.emit(status)

    @Slot(str)
    def set_transcript(self, transcript: str) -> None:
        if self.transcript == transcript:
            return
        self.transcript = transcript
        self.transcript_changed.emit(transcript)

    @Slot(str)
    def set_response(self, response: str) -> None:
        if self.response == response:
            return
        self.response = response
        self.response_changed.emit(response)

    @Slot(float)
    def set_mic_level(self, level: float) -> None:
        level = max(0.0, min(1.0, float(level)))
        self.mic_level = level
        self.mic_level_changed.emit(level)

    def add_log(self, message: str, level: str = "INFO", source: str = "UI") -> None:
        entry = LogEntry(
            timestamp=self._timestamp(),
            level=level.upper(),
            source=source,
            message=message,
        )
        self.logs.append(entry)
        _persist_log_entry(entry)
        self.log_added.emit(asdict(entry))
        self.logs_changed.emit()

    def set_metrics(self, metrics: dict[str, Any]) -> None:
        self.metrics.update(metrics)
        self.metrics_changed.emit(dict(self.metrics))

    def set_backend_online(self, online: bool) -> None:
        if self.backend_online == online:
            return
        self.backend_online = online
        self.backend_online_changed.emit(online)

    def set_session_running(self, running: bool) -> None:
        if self.session_running == running:
            return
        self.session_running = running
        self.session_running_changed.emit(running)

    def set_can_interrupt(self, can_interrupt: bool) -> None:
        if self.can_interrupt == can_interrupt:
            return
        self.can_interrupt = can_interrupt
        self.interrupt_capability_changed.emit(can_interrupt)

    def set_perf_mode(self, low_perf: bool) -> None:
        if self.low_perf == low_perf:
            return
        self.low_perf = low_perf
        self.perf_mode_changed.emit(low_perf)

    def set_safety_feedback(
        self,
        *,
        safety_level: str | None = None,
        reason: str | None = None,
        activity: str | None = None,
        scope: str | None = None,
        confirmation_id: str | None = None,
    ) -> None:
        changed = False
        if safety_level is not None and self.safety_feedback.get("safety_level") != safety_level:
            self.safety_feedback["safety_level"] = safety_level
            changed = True
        if reason is not None and self.safety_feedback.get("reason") != reason:
            self.safety_feedback["reason"] = reason
            changed = True
        if activity is not None and self.safety_feedback.get("activity") != activity:
            self.safety_feedback["activity"] = activity
            changed = True
        if scope is not None and self.safety_feedback.get("scope") != scope:
            self.safety_feedback["scope"] = scope
            changed = True
        if confirmation_id is not None:
            self.safety_feedback["confirmation_id"] = confirmation_id
            changed = True
        if changed:
            self.safety_feedback_changed.emit(dict(self.safety_feedback))

    def set_model_status(self, component: str, value: str) -> None:
        self.model_status[component] = value
        self.model_status_changed.emit(dict(self.model_status))

    def update_devices(
        self,
        *,
        inputs: list[dict[str, Any]] | None = None,
        outputs: list[dict[str, Any]] | None = None,
        selected_input: int | None | object = Ellipsis,
        selected_output: int | None | object = Ellipsis,
    ) -> None:
        if inputs is not None:
            self.devices["inputs"] = inputs
        if outputs is not None:
            self.devices["outputs"] = outputs
        if selected_input is not Ellipsis:
            self.devices["selected_input"] = selected_input
        if selected_output is not Ellipsis:
            self.devices["selected_output"] = selected_output
        self.devices_changed.emit(dict(self.devices))

    def push_error(self, message: str, source: str = "system") -> None:
        item = {"timestamp": self._timestamp(), "source": source, "message": message}
        self.errors.append(item)
        self.errors_changed.emit()
        self.add_log(message, level="ERROR", source=source)

    def push_toast(self, title: str, message: str, tone: str = "info") -> None:
        self.toast_requested.emit(title, message, tone)

    def append_turn(self, user_text: str, assistant_text: str) -> None:
        self.history.append(
            {
                "timestamp": self._timestamp(),
                "user": user_text.strip(),
                "assistant": assistant_text.strip(),
            }
        )
        self.history_changed.emit()


state = AppState.get_instance()


_ui_logger = logging.getLogger("Jarvis.UI")
_ui_error_logger = logging.getLogger("Jarvis.Errors")


def _persist_log_entry(entry: LogEntry) -> None:
    message = f"[{entry.source}] {entry.message}"
    if entry.level in {"ERROR", "CRITICAL"}:
        _ui_error_logger.error(message)
    elif entry.level in {"WARN", "WARNING"}:
        _ui_logger.warning(message)
    elif entry.level == "DEBUG":
        _ui_logger.debug(message)
    else:
        _ui_logger.info(message)
