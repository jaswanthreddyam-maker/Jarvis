"""
Jarvis Overlay Chat - premium text-input and short-history widgets.

These widgets live inside the full-screen overlay and keep the presentation
lightweight so the orb remains the visual focus.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


@dataclass(slots=True)
class ChatMessage:
    role: str
    text: str
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%H:%M"))


_INPUT_STYLE = """
QLineEdit {
    background: rgba(14, 18, 24, 210);
    color: #f4ecdf;
    border: 1px solid rgba(184, 155, 92, 0.35);
    border-radius: 22px;
    padding: 10px 20px 10px 20px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px;
    font-weight: 500;
    selection-background-color: rgba(184, 155, 92, 0.40);
}
QLineEdit:focus {
    border: 1.5px solid rgba(184, 155, 92, 0.65);
    background: rgba(14, 18, 24, 230);
}
QLineEdit::placeholder {
    color: rgba(130, 140, 150, 0.60);
}
"""

_USER_BUBBLE = """
QLabel {
    background: rgba(184, 155, 92, 0.15);
    color: #f4ecdf;
    border: 1px solid rgba(184, 155, 92, 0.25);
    border-radius: 14px;
    padding: 8px 14px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}
"""

_ASSISTANT_BUBBLE = """
QLabel {
    background: rgba(80, 200, 180, 0.10);
    color: #d0e8e4;
    border: 1px solid rgba(80, 200, 180, 0.20);
    border-radius: 14px;
    padding: 8px 14px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}
"""

_THINKING_STYLE = """
QLabel {
    background: rgba(160, 100, 220, 0.12);
    color: rgba(200, 180, 240, 0.8);
    border: 1px solid rgba(160, 100, 220, 0.2);
    border-radius: 14px;
    padding: 8px 14px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
    font-style: italic;
}
"""


class _ChatBubble(QFrame):
    """Single chat message."""

    def __init__(self, message: ChatMessage, parent=None):
        super().__init__(parent)
        self._message = message

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        label = QLabel(message.text)
        label.setWordWrap(True)
        label.setMaximumWidth(460)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        if message.role == "user":
            label.setStyleSheet(_USER_BUBBLE)
            layout.addStretch()
            layout.addWidget(label)
        elif message.role == "thinking":
            label.setStyleSheet(_THINKING_STYLE)
            layout.addWidget(label)
            layout.addStretch()
        else:
            label.setStyleSheet(_ASSISTANT_BUBBLE)
            layout.addWidget(label)
            layout.addStretch()

        self._label = label

    def update_text(self, text: str) -> None:
        self._message.text = text
        self._label.setText(text)


class ChatHistory(QScrollArea):
    """Compact scrollable history capped to keep the overlay uncluttered."""

    MAX_VISIBLE = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("background: transparent;")
        self.setMaximumHeight(260)
        self.setMinimumHeight(120)

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.addStretch()
        self.setWidget(self._container)

        self._bubbles: deque[_ChatBubble] = deque()
        self._thinking_bubble: _ChatBubble | None = None
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(400)
        self._dot_timer.timeout.connect(self._animate_dots)
        self._dot_count = 0

    def add_message(self, message: ChatMessage) -> _ChatBubble:
        self._remove_thinking()

        bubble = _ChatBubble(message, self._container)
        self._layout.insertWidget(self._layout.count() - 1, bubble)
        self._bubbles.append(bubble)

        while len(self._bubbles) > self.MAX_VISIBLE:
            old = self._bubbles.popleft()
            self._layout.removeWidget(old)
            old.deleteLater()

        QTimer.singleShot(50, self._scroll_bottom)
        return bubble

    def show_thinking(self) -> None:
        self._remove_thinking()
        self._thinking_bubble = _ChatBubble(
            ChatMessage(role="thinking", text="Thinking..."),
            self._container,
        )
        self._layout.insertWidget(self._layout.count() - 1, self._thinking_bubble)
        self._dot_count = 0
        self._dot_timer.start()
        QTimer.singleShot(50, self._scroll_bottom)

    def clear(self) -> None:
        self._remove_thinking()
        for bubble in self._bubbles:
            self._layout.removeWidget(bubble)
            bubble.deleteLater()
        self._bubbles.clear()

    def _animate_dots(self) -> None:
        if self._thinking_bubble is None:
            self._dot_timer.stop()
            return
        self._dot_count = (self._dot_count + 1) % 4
        dots = "." * (self._dot_count + 1)
        self._thinking_bubble.update_text(f"Thinking{dots}")

    def _remove_thinking(self) -> None:
        if self._thinking_bubble is not None:
            self._layout.removeWidget(self._thinking_bubble)
            self._thinking_bubble.deleteLater()
            self._thinking_bubble = None
        if self._dot_timer.isActive():
            self._dot_timer.stop()

    def _scroll_bottom(self) -> None:
        scrollbar = self.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class ChatInputField(QLineEdit):
    """Styled text input that emits submitted(text) on Enter."""

    submitted = Signal(str)
    draft_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Ask Jarvis anything...")
        self.setStyleSheet(_INPUT_STYLE)
        self.setFixedHeight(44)
        self.setMaximumWidth(520)
        self.setMinimumWidth(360)
        self.setFont(QFont("Segoe UI", 14))
        self.setClearButtonEnabled(False)

        self.returnPressed.connect(self._on_submit)
        self.textChanged.connect(self.draft_changed)

    def _on_submit(self) -> None:
        text = self.text().strip()
        if not text:
            return
        self.submitted.emit(text)
        self.clear()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)


class OverlayChatPanel(QWidget):
    """Combined widget: compact history plus a single-line input field."""

    text_submitted = Signal(str)
    draft_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setFixedWidth(540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(20)

        self.history = ChatHistory(self)
        self.input_field = ChatInputField(self)

        layout.addWidget(self.history, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.input_field, 0, Qt.AlignmentFlag.AlignHCenter)

        self.input_field.submitted.connect(self.text_submitted)
        self.input_field.draft_changed.connect(self.draft_changed)

    def fade_in(self) -> None:
        self.show()

    def fade_out(self) -> None:
        self.hide()

    def focus_input(self) -> None:
        self.input_field.setFocus(Qt.FocusReason.OtherFocusReason)
        self.input_field.activateWindow()

    def set_processing(self, processing: bool) -> None:
        self.input_field.setEnabled(not processing)
        self.input_field.setReadOnly(processing)
        if processing:
            self.input_field.setPlaceholderText("Processing...")
        else:
            self.input_field.setPlaceholderText("Ask Jarvis anything...")

    def ready_for_next_turn(self) -> None:
        self.input_field.setEnabled(True)
        self.input_field.setReadOnly(False)
        self.input_field.clear()
        self.input_field.setPlaceholderText("Ask Jarvis anything...")
        self.focus_input()

    def add_user_message(self, text: str) -> _ChatBubble:
        return self.history.add_message(ChatMessage(role="user", text=text))

    def add_assistant_message(self, text: str) -> _ChatBubble:
        return self.history.add_message(ChatMessage(role="assistant", text=text))

    def show_thinking(self) -> None:
        self.history.show_thinking()

    def current_text(self) -> str:
        return self.input_field.text()

    def reset(self) -> None:
        self.history.clear()
        self.input_field.clear()
        self.input_field.setEnabled(True)
        self.input_field.setReadOnly(False)
        self.input_field.setPlaceholderText("Ask Jarvis anything...")
