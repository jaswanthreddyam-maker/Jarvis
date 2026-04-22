from __future__ import annotations

from PySide6.QtCore import QSize, QTimer, Signal
from PySide6.QtWidgets import QLabel, QFrame, QSizePolicy


class StreamingTextLabel(QLabel):
    stream_completed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._index = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setWordWrap(True)
        self.setTextInteractionFlags(self.textInteractionFlags())
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFrameShadow(QFrame.Shadow.Plain)
        self.setStyleSheet("background: transparent; border: none; color: rgba(255,255,255,0.75);")
        self.hide()

    def sizeHint(self) -> QSize:
        return QSize(400, 56)

    def minimumSizeHint(self) -> QSize:
        return QSize(200, 32)

    def stream_text(self, text: str) -> None:
        self._timer.stop()
        self._full_text = text or ""
        self._index = 0
        self.setText("")
        if not self._full_text:
            self.hide()
            self.stream_completed.emit()
            return
        self.show()
        self._timer.start(self._interval_for_text(self._full_text))

    def extend_text(self, full_text: str) -> None:
        """Continue streaming from the current index for incremental updates."""
        full_text = full_text or ""
        if full_text == self._full_text:
            return
        if not full_text.startswith(self.text()):
            self.stream_text(full_text)
            return
        self._full_text = full_text
        if not self._timer.isActive() and self._index < len(self._full_text):
            self._timer.start(self._interval_for_text(self._full_text))

    def set_text_now(self, text: str) -> None:
        self._timer.stop()
        self._full_text = text or ""
        self._index = len(self._full_text)
        self.setText(self._full_text)
        self.setVisible(bool(self._full_text))
        self.stream_completed.emit()

    def clear_stream(self) -> None:
        self._timer.stop()
        self._full_text = ""
        self._index = 0
        self.setText("")
        self.hide()

    def _interval_for_text(self, text: str) -> int:
        if len(text) < 60:
            return 20
        if len(text) < 180:
            return 14
        return 10

    def _tick(self) -> None:
        if self._index >= len(self._full_text):
            self._timer.stop()
            self.stream_completed.emit()
            return

        remaining = len(self._full_text) - self._index
        step = 1 if remaining < 40 else 2 if remaining < 140 else 4
        self._index = min(len(self._full_text), self._index + step)
        self.setText(self._full_text[: self._index])
