from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QFrame, QLabel, QTextBrowser, QVBoxLayout

from ui.state import AppState
from ui.widgets.streaming_text import StreamingTextLabel


class TranscriptPanel(QFrame):
    def __init__(self, state: AppState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self._last_response = ""
        self._rendered_turn_count = 0
        self.setObjectName("panel")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        eyebrow = QLabel("TRANSCRIPT")
        eyebrow.setObjectName("sectionEyebrow")
        title = QLabel("Live conversation stream")
        title.setObjectName("panelTitle")

        self._history = QTextBrowser()
        self._history.setObjectName("historyView")
        self._history.setOpenExternalLinks(False)
        self._history.setFrameShape(QFrame.Shape.NoFrame)
        self._history.setReadOnly(True)

        heard_title = QLabel("Last Heard")
        heard_title.setObjectName("subsectionTitle")
        self._transcript = QLabel("No speech captured yet.")
        self._transcript.setObjectName("transcriptCard")
        self._transcript.setWordWrap(True)
        self._transcript.setAlignment(Qt.AlignmentFlag.AlignTop)

        response_title = QLabel("Jarvis Response")
        response_title.setObjectName("subsectionTitle")
        self._safety = QLabel("Safety: SAFE")
        self._safety.setObjectName("mutedText")
        self._safety.setWordWrap(True)
        self._response = StreamingTextLabel()
        self._response.setObjectName("assistantResponse")
        self._response.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        root.addWidget(eyebrow)
        root.addWidget(title)
        root.addWidget(self._history, 1)
        root.addWidget(heard_title)
        root.addWidget(self._transcript)
        root.addWidget(response_title)
        root.addWidget(self._safety)
        root.addWidget(self._response)

        state.transcript_changed.connect(self._on_transcript_changed)
        state.response_changed.connect(self._on_response_changed)
        state.history_changed.connect(self._render_history)
        state.safety_feedback_changed.connect(self._on_safety_feedback_changed)

        self._render_history()
        self._on_transcript_changed(state.transcript)
        self._on_response_changed(state.response)
        self._on_safety_feedback_changed(state.safety_feedback)

    def _on_transcript_changed(self, transcript: str) -> None:
        self._transcript.setText(transcript or "No speech captured yet.")

    def _on_response_changed(self, response: str) -> None:
        if not response or response == self._last_response:
            return
        if response.startswith(self._last_response):
            self._response.extend_text(response)
        else:
            self._response.stream_text(response)
        self._last_response = response

    def _build_style_block(self) -> str:
        return """
        <style>
            body { color: #E6DED0; font-family: 'Segoe UI'; }
            .turn { margin-bottom: 18px; }
            .stamp { color: #8B8F97; font-size: 11px; letter-spacing: 0.6px; margin-bottom: 6px; }
            .speaker { color: #B89B5C; font-size: 11px; font-weight: 700; letter-spacing: 0.9px; margin-bottom: 4px; text-transform: uppercase; }
            .bubble { padding: 10px 12px; border-radius: 12px; margin-bottom: 10px; line-height: 1.5; }
            .bubble.user { background: rgba(255,255,255,0.04); color: #F2E9D8; }
            .bubble.assistant { background: rgba(184,155,92,0.08); color: #F7F3EC; border: 1px solid rgba(184,155,92,0.16); }
            .placeholder { color: #8B8F97; line-height: 1.5; }
        </style>
        """

    def _build_turn_html(self, turn: dict[str, str]) -> str:
        user_text = html.escape(turn.get("user", ""))
        assistant_text = html.escape(turn.get("assistant", ""))
        timestamp = html.escape(turn.get("timestamp", ""))
        return (
            f"<div class=\"turn\">"
            f"<div class=\"stamp\">{timestamp}</div>"
            f"<div class=\"speaker\">User</div>"
            f"<div class=\"bubble user\">{user_text or '...'}</div>"
            f"<div class=\"speaker\">Jarvis</div>"
            f"<div class=\"bubble assistant\">{assistant_text or '...'}</div>"
            f"</div>"
        )

    def _render_history(self) -> None:
        history = list(self._state.history)
        if not history:
            self._history.setHtml(
                "<div class='placeholder'>Waiting for the first exchange. Logs and responses appear here in real time.</div>"
            )
            self._rendered_turn_count = 0
            return

        if self._rendered_turn_count > len(history):
            self._rendered_turn_count = 0

        if self._rendered_turn_count == 0:
            self._history.setHtml(self._build_style_block())

        new_turns = history[self._rendered_turn_count:]
        if not new_turns:
            return

        cursor = self._history.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        for turn in new_turns:
            cursor.insertHtml(self._build_turn_html(turn))
            cursor.insertHtml("<br/>")
        self._history.setTextCursor(cursor)
        self._rendered_turn_count = len(history)
        self._history.verticalScrollBar().setValue(self._history.verticalScrollBar().maximum())

    def _on_safety_feedback_changed(self, payload: dict[str, str]) -> None:
        safety_level = payload.get("safety_level", "SAFE") or "SAFE"
        activity = payload.get("activity", "Idle") or "Idle"
        reason = payload.get("reason", "Awaiting validated work.") or "Awaiting validated work."
        self._safety.setText(f"Safety: {safety_level} | Activity: {activity} | {reason}")
