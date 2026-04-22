from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QPlainTextEdit, QVBoxLayout

from ui.state import AppState


class LogsPanel(QFrame):
    def __init__(self, state: AppState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self._state = state

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        eyebrow = QLabel("DEBUG")
        eyebrow.setObjectName("sectionEyebrow")
        title = QLabel("Runtime logs")
        title.setObjectName("panelTitle")

        self._view = QPlainTextEdit()
        self._view.setObjectName("logsView")
        self._view.setReadOnly(True)

        root.addWidget(eyebrow)
        root.addWidget(title)
        root.addWidget(self._view, 1)

        for entry in state.logs:
            self._append_entry(
                {
                    "timestamp": entry.timestamp,
                    "level": entry.level,
                    "source": entry.source,
                    "message": entry.message,
                }
            )
        state.log_added.connect(self._append_entry)

    def _append_entry(self, entry: dict) -> None:
        line = f"[{entry['timestamp']}] {entry['source']:<10} {entry['level']:<7} {entry['message']}"
        self._view.appendPlainText(line)
