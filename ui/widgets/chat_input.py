"""
Jarvis overlay chat widgets.

The overlay now uses two focused surfaces:
- a centered input bar with a voice trigger button
- a dedicated right-side conversation panel with status feedback
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
    QPushButton,
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
    pending: bool = False


_STATUS_META = {
    "IDLE": {
        "label": "Idle",
        "detail": "Awaiting the next request",
        "dot": "#7d8792",
        "text": "#aeb6bf",
    },
    "TYPING": {
        "label": "Typing",
        "detail": "Drafting a text request",
        "dot": "#b8960c",
        "text": "#e9d8a3",
    },
    "LISTENING": {
        "label": "Listening",
        "detail": "Waiting for speech or text",
        "dot": "#4ade80",
        "text": "#d8f7e0",
    },
    "RECOGNIZING": {
        "label": "Thinking",
        "detail": "Understanding the request",
        "dot": "#60a5fa",
        "text": "#d9e8ff",
    },
    "THINKING": {
        "label": "Thinking",
        "detail": "Routing and planning",
        "dot": "#60a5fa",
        "text": "#d9e8ff",
    },
    "PROCESSING": {
        "label": "Executing",
        "detail": "Preparing the next tool step",
        "dot": "#a78bfa",
        "text": "#e6dcff",
    },
    "EXECUTING": {
        "label": "Executing",
        "detail": "Running validated actions",
        "dot": "#a78bfa",
        "text": "#e6dcff",
    },
    "RESPONDING": {
        "label": "Responding",
        "detail": "Finalizing the answer",
        "dot": "#a78bfa",
        "text": "#e6dcff",
    },
    "SPEAKING": {
        "label": "Speaking",
        "detail": "Delivering the reply",
        "dot": "#b8960c",
        "text": "#f3e3b7",
    },
    "INTERRUPTED": {
        "label": "Interrupted",
        "detail": "Request stopped",
        "dot": "#f97316",
        "text": "#ffe1c7",
    },
    "ERROR": {
        "label": "Error",
        "detail": "A request needs attention",
        "dot": "#ef4444",
        "text": "#ffd8d8",
    },
}


def status_palette(status: str) -> dict[str, str]:
    upper = (status or "IDLE").strip().upper()
    return dict(_STATUS_META.get(upper, _STATUS_META["IDLE"]))


_INPUT_SHELL_STYLE = """
QFrame {
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(184, 150, 12, 0.40);
    border-radius: 28px;
}
"""

_INPUT_FIELD_STYLE = """
QLineEdit {
    background: transparent;
    color: rgba(255, 255, 255, 0.92);
    border: none;
    padding: 0px;
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
    font-weight: 500;
    selection-background-color: rgba(184, 150, 12, 0.35);
}
QLineEdit::placeholder {
    color: rgba(255, 255, 255, 0.30);
}
"""

_VOICE_BUTTON_STYLE = """
QPushButton {
    background: rgba(184, 150, 12, 0.18);
    color: #f4ecdf;
    border: 1px solid rgba(184, 150, 12, 0.42);
    border-radius: 12px;
    padding: 0px 10px;
    font-size: 11px;
    font-weight: 700;
}
QPushButton:hover {
    background: rgba(184, 150, 12, 0.28);
}
QPushButton:disabled {
    color: rgba(255, 255, 255, 0.24);
    background: rgba(255, 255, 255, 0.04);
    border-color: rgba(255, 255, 255, 0.10);
}
"""


class _ConversationBubble(QFrame):
    """Single right-panel bubble with role-aware styling."""

    def __init__(self, message: ChatMessage, parent=None):
        super().__init__(parent)
        self._message = message
        self._pending = bool(message.pending)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        shell = QFrame(self)
        shell.setObjectName("conversationBubbleShell")
        shell.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        shell.setMaximumWidth(236)

        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(10, 8, 10, 8)
        shell_layout.setSpacing(3)

        tag = QLabel("You" if message.role == "user" else "Jarvis", shell)
        tag.setWordWrap(False)

        body = QLabel(message.text, shell)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        shell_layout.addWidget(tag)
        shell_layout.addWidget(body)

        if message.role == "user":
            outer.addStretch()
            outer.addWidget(shell)
        else:
            outer.addWidget(shell)
            outer.addStretch()

        self._shell = shell
        self._tag = tag
        self._body = body
        self._apply_style()

    @property
    def is_pending(self) -> bool:
        return self._pending

    def update_text(self, text: str) -> None:
        self._message.text = text
        self._body.setText(text)

    def set_pending(self, pending: bool) -> None:
        self._pending = bool(pending)
        self._message.pending = self._pending
        self._apply_style()

    def _apply_style(self) -> None:
        if self._message.role == "user":
            shell_style = (
                "background: rgba(184, 150, 12, 0.12);"
                "border: 1px solid rgba(184, 150, 12, 0.24);"
                "border-radius: 10px;"
            )
            tag_style = "color: rgba(255, 255, 255, 0.25); font-size: 9px; font-weight: 600;"
            body_style = (
                "color: rgba(255, 255, 255, 0.76);"
                "font-size: 11px;"
                "line-height: 1.45;"
            )
        elif self._pending:
            shell_style = (
                "background: rgba(255, 255, 255, 0.03);"
                "border: 1px solid rgba(255, 255, 255, 0.08);"
                "border-radius: 10px;"
            )
            tag_style = "color: rgba(255, 255, 255, 0.25); font-size: 9px; font-weight: 600;"
            body_style = (
                "color: rgba(150, 200, 180, 0.82);"
                "font-size: 10px;"
                "line-height: 1.55;"
                "font-family: 'Cascadia Mono', 'Consolas', monospace;"
            )
        else:
            shell_style = (
                "background: rgba(255, 255, 255, 0.05);"
                "border: 1px solid rgba(255, 255, 255, 0.08);"
                "border-radius: 10px;"
            )
            tag_style = "color: rgba(255, 255, 255, 0.25); font-size: 9px; font-weight: 600;"
            body_style = (
                "color: rgba(255, 255, 255, 0.88);"
                "font-size: 11px;"
                "line-height: 1.55;"
            )

        self._shell.setStyleSheet(shell_style)
        self._tag.setStyleSheet(tag_style)
        self._body.setStyleSheet(body_style)


class _ConversationHistory(QScrollArea):
    MAX_VISIBLE = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("background: transparent; border: none;")

        container = QWidget(self)
        container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 2, 0)
        layout.setSpacing(10)

        placeholder = QLabel(
            "Conversation turns appear here while Jarvis listens, reasons, and acts.",
            container,
        )
        placeholder.setWordWrap(True)
        placeholder.setStyleSheet(
            "color: rgba(255, 255, 255, 0.30);"
            "font-size: 11px;"
            "line-height: 1.5;"
        )

        layout.addWidget(placeholder)
        layout.addStretch()
        self.setWidget(container)

        self._container = container
        self._layout = layout
        self._placeholder = placeholder
        self._bubbles: deque[_ConversationBubble] = deque()
        self._pending_assistant: _ConversationBubble | None = None

    def add_message(self, message: ChatMessage) -> _ConversationBubble:
        self._hide_placeholder()
        bubble = _ConversationBubble(message, self._container)
        self._layout.insertWidget(self._layout.count() - 1, bubble)
        self._bubbles.append(bubble)

        while len(self._bubbles) > self.MAX_VISIBLE:
            old = self._bubbles.popleft()
            if old is self._pending_assistant:
                self._pending_assistant = None
            self._layout.removeWidget(old)
            old.deleteLater()

        QTimer.singleShot(0, self._scroll_bottom)
        return bubble

    def add_user_message(self, text: str) -> _ConversationBubble:
        return self.add_message(ChatMessage(role="user", text=text.strip()))

    def begin_assistant_message(self, text: str) -> _ConversationBubble:
        if self._pending_assistant is None:
            self._pending_assistant = self.add_message(
                ChatMessage(role="assistant", text=text, pending=True)
            )
        else:
            self._pending_assistant.update_text(text)
            self._pending_assistant.set_pending(True)
        QTimer.singleShot(0, self._scroll_bottom)
        return self._pending_assistant

    def update_assistant_message(self, text: str) -> _ConversationBubble:
        return self.begin_assistant_message(text)

    def finalize_assistant_message(self, text: str | None = None) -> None:
        if self._pending_assistant is None:
            if text:
                self.add_message(ChatMessage(role="assistant", text=text.strip()))
            return

        if text is not None and text.strip():
            self._pending_assistant.update_text(text.strip())
        self._pending_assistant.set_pending(False)
        self._pending_assistant = None
        QTimer.singleShot(0, self._scroll_bottom)

    def set_pending_hint(self, text: str) -> None:
        hint = text.strip()
        if not hint:
            return
        self.begin_assistant_message(f"-> {hint}")

    def clear(self) -> None:
        self._pending_assistant = None
        for bubble in self._bubbles:
            self._layout.removeWidget(bubble)
            bubble.deleteLater()
        self._bubbles.clear()
        self._placeholder.show()

    def has_pending_assistant(self) -> bool:
        return self._pending_assistant is not None

    def pending_text(self) -> str:
        if self._pending_assistant is None:
            return ""
        return self._pending_assistant._message.text

    def _hide_placeholder(self) -> None:
        if self._placeholder.isVisible():
            self._placeholder.hide()

    def _scroll_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


class _StatusStrip(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "background: rgba(255, 255, 255, 0.03);"
            "border: 1px solid rgba(255, 255, 255, 0.06);"
            "border-radius: 8px;"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(7)

        dot = QLabel(self)
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("background: #7d8792; border-radius: 4px;")

        name = QLabel("Idle", self)
        name.setStyleSheet(
            "color: rgba(255, 255, 255, 0.52);"
            "font-size: 10px;"
            "font-weight: 700;"
            "letter-spacing: 0.8px;"
            "text-transform: uppercase;"
        )

        detail = QLabel("Awaiting the next request", self)
        detail.setWordWrap(True)
        detail.setStyleSheet(
            "color: rgba(255, 255, 255, 0.28);"
            "font-size: 10px;"
            "line-height: 1.4;"
        )

        top.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(name, 1)
        layout.addLayout(top)
        layout.addWidget(detail)

        self._dot = dot
        self._name = name
        self._detail = detail
        self.set_status("IDLE")

    def set_status(self, status: str, detail: str | None = None) -> None:
        meta = status_palette(status)
        self._dot.setStyleSheet(f"background: {meta['dot']}; border-radius: 4px;")
        self._name.setText(meta["label"].upper())
        self._name.setStyleSheet(
            f"color: {meta['text']};"
            "font-size: 10px;"
            "font-weight: 700;"
            "letter-spacing: 0.8px;"
            "text-transform: uppercase;"
        )
        self._detail.setText(detail or meta["detail"])


class ChatInputField(QLineEdit):
    submitted = Signal(str)
    draft_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Ask Jarvis anything...")
        self.setStyleSheet(_INPUT_FIELD_STYLE)
        self.setFixedHeight(24)
        self.setFont(QFont("Segoe UI", 13))
        self.setFrame(False)
        self.setClearButtonEnabled(False)

        self.returnPressed.connect(self._on_submit)
        self.textChanged.connect(self.draft_changed)

    def _on_submit(self) -> None:
        text = self.text().strip()
        if not text:
            return
        self.submitted.emit(text)
        self.clear()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)


class OverlayChatPanel(QWidget):
    """Centered input bar used under the orb."""

    text_submitted = Signal(str)
    draft_changed = Signal(str)
    voice_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setFixedWidth(360)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        shell = QFrame(self)
        shell.setStyleSheet(_INPUT_SHELL_STYLE)
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(16, 10, 10, 10)
        shell_layout.setSpacing(10)

        input_field = ChatInputField(shell)
        voice_button = QPushButton("Mic", shell)
        voice_button.setCursor(Qt.CursorShape.PointingHandCursor)
        voice_button.setFixedSize(42, 24)
        voice_button.setStyleSheet(_VOICE_BUTTON_STYLE)

        shell_layout.addWidget(input_field, 1)
        shell_layout.addWidget(voice_button, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(shell)

        self._shell = shell
        self.input_field = input_field
        self._voice_button = voice_button

        self.input_field.submitted.connect(self.text_submitted)
        self.input_field.draft_changed.connect(self.draft_changed)
        self._voice_button.clicked.connect(self.voice_requested.emit)

    def focus_input(self) -> None:
        self.input_field.setFocus(Qt.FocusReason.OtherFocusReason)
        self.input_field.activateWindow()

    def set_processing(self, processing: bool) -> None:
        self.input_field.setEnabled(not processing)
        self.input_field.setReadOnly(processing)
        self._voice_button.setEnabled(not processing)
        self.input_field.setPlaceholderText(
            "Jarvis is working..." if processing else "Ask Jarvis anything..."
        )

    def ready_for_next_turn(self) -> None:
        self.input_field.setEnabled(True)
        self.input_field.setReadOnly(False)
        self._voice_button.setEnabled(True)
        self.input_field.clear()
        self.input_field.setPlaceholderText("Ask Jarvis anything...")
        self.focus_input()

    def current_text(self) -> str:
        return self.input_field.text()

    def reset(self) -> None:
        self.input_field.clear()
        self.input_field.setEnabled(True)
        self.input_field.setReadOnly(False)
        self._voice_button.setEnabled(True)
        self.input_field.setPlaceholderText("Ask Jarvis anything...")


class OverlayConversationPanel(QFrame):
    """Right-side panel with turn history and runtime status."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("overlayConversationPanel")
        self.setFixedWidth(320)
        self.setStyleSheet(
            "#overlayConversationPanel {"
            "  background: rgba(255, 255, 255, 0.03);"
            "  border: 1px solid rgba(255, 255, 255, 0.07);"
            "  border-radius: 18px;"
            "}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 18, 16, 16)
        root.setSpacing(14)

        header = QLabel("Conversation", self)
        header.setStyleSheet(
            "color: rgba(255, 255, 255, 0.28);"
            "font-size: 10px;"
            "font-weight: 700;"
            "letter-spacing: 1.0px;"
            "text-transform: uppercase;"
            "padding-bottom: 6px;"
        )

        history = _ConversationHistory(self)
        status_strip = _StatusStrip(self)

        root.addWidget(header)
        root.addWidget(history, 1)
        root.addWidget(status_strip)

        self.history = history
        self.status_strip = status_strip

    def add_user_message(self, text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            self.history.add_user_message(cleaned)

    def set_pending_assistant_hint(self, text: str) -> None:
        self.history.set_pending_hint(text)

    def update_assistant_message(self, text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            self.history.update_assistant_message(cleaned)

    def finalize_assistant_message(self, text: str | None = None) -> None:
        self.history.finalize_assistant_message(text)

    def has_pending_assistant(self) -> bool:
        return self.history.has_pending_assistant()

    def pending_text(self) -> str:
        return self.history.pending_text()

    def set_status(self, status: str, detail: str | None = None) -> None:
        self.status_strip.set_status(status, detail)

    def reset(self) -> None:
        self.history.clear()
        self.status_strip.set_status("IDLE")
