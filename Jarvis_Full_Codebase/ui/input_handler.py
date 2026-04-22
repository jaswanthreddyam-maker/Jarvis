from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QObject, Signal, Slot


class OverlaySessionState(str, Enum):
    IDLE = "IDLE"
    TYPING = "TYPING"
    PROCESSING = "PROCESSING"
    EXECUTING = "EXECUTING"
    RESPONDING = "RESPONDING"


_BUSY_STATES = {
    OverlaySessionState.PROCESSING,
    OverlaySessionState.EXECUTING,
    OverlaySessionState.RESPONDING,
}


class OverlayInputHandler(QObject):
    """Tracks lightweight text-overlay state without blocking the UI thread."""

    state_changed = Signal(str)
    submission_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = OverlaySessionState.IDLE
        self._draft = ""

    @property
    def state(self) -> OverlaySessionState:
        return self._state

    @property
    def is_busy(self) -> bool:
        return self._state in _BUSY_STATES

    @Slot()
    def activate(self) -> None:
        if self.is_busy:
            return
        self._set_state(
            OverlaySessionState.TYPING if self._draft.strip() else OverlaySessionState.IDLE
        )

    @Slot(str)
    def on_draft_changed(self, text: str) -> None:
        self._draft = text
        if self.is_busy:
            return
        self._set_state(
            OverlaySessionState.TYPING if text.strip() else OverlaySessionState.IDLE
        )

    @Slot(str)
    def submit(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned or self.is_busy:
            return
        self._draft = ""
        self._set_state(OverlaySessionState.PROCESSING)
        self.submission_requested.emit(cleaned)

    def mark_processing(self) -> None:
        self._set_state(OverlaySessionState.PROCESSING)

    def mark_executing(self) -> None:
        self._set_state(OverlaySessionState.EXECUTING)

    def mark_responding(self) -> None:
        self._set_state(OverlaySessionState.RESPONDING)

    @Slot()
    def finish_cycle(self) -> None:
        self._set_state(
            OverlaySessionState.TYPING if self._draft.strip() else OverlaySessionState.IDLE
        )

    @Slot()
    def cancel(self) -> None:
        self._set_state(
            OverlaySessionState.TYPING if self._draft.strip() else OverlaySessionState.IDLE
        )

    def _set_state(self, state: OverlaySessionState) -> None:
        if self._state == state:
            return
        self._state = state
        self.state_changed.emit(state.value)
