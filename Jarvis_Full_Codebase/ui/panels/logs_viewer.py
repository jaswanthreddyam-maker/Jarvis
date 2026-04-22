"""Logs Viewer — standalone dialog accessible from the system tray.

Displays a scrollable, filterable log of all Jarvis runtime events.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class LogsViewerDialog(QDialog):
    """Floating logs viewer window for the system tray."""

    def __init__(self, state, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self._filter_text = ""

        self.setWindowTitle("Jarvis — System Logs")
        self.setMinimumSize(720, 480)
        self.resize(860, 560)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.setStyleSheet(
            "QDialog { background: #0e1218; }"
            "QLabel { color: #f4ecdf; }"
            "QLineEdit { background: #1a2030; color: #f4ecdf; border: 1px solid #2a3444; "
            "border-radius: 6px; padding: 6px 10px; font-size: 12px; }"
            "QLineEdit:focus { border-color: #b89b5c; }"
            "QPushButton { background: rgba(184,155,92,0.15); color: #f4ecdf; "
            "border: 1px solid rgba(184,155,92,0.3); border-radius: 6px; "
            "padding: 6px 14px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: rgba(184,155,92,0.25); }"
            "QTextEdit { background: #12171d; color: #c8d0da; "
            "border: 1px solid #1e2838; border-radius: 8px; "
            "padding: 8px; font-family: 'Cascadia Code', 'Consolas', monospace; "
            "font-size: 11px; selection-background-color: #b89b5c; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # Header
        header = QLabel("SYSTEM LOGS")
        header.setStyleSheet(
            "font-size: 14px; font-weight: 700; color: #b89b5c; letter-spacing: 2px;"
        )
        layout.addWidget(header)

        # Filter bar
        filter_bar = QWidget()
        filter_layout = QHBoxLayout(filter_bar)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(8)

        self._filter_input = QLineEdit()
        self._filter_input.setPlaceholderText("Filter logs…")
        self._filter_input.textChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self._filter_input, 1)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_logs)
        filter_layout.addWidget(clear_btn)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_logs)
        filter_layout.addWidget(refresh_btn)

        layout.addWidget(filter_bar)

        # Log display
        self._log_display = QTextEdit()
        self._log_display.setReadOnly(True)
        self._log_display.setFont(QFont("Cascadia Code", 10))
        layout.addWidget(self._log_display, 1)

        # Status bar
        self._status = QLabel("0 entries")
        self._status.setStyleSheet("color: #556677; font-size: 10px;")
        layout.addWidget(self._status)

        # Connect to live log updates
        self.state.log_added.connect(self._on_log_added)

        # Load existing logs
        self._refresh_logs()

    @Slot(str)
    def _on_filter_changed(self, text: str) -> None:
        self._filter_text = text.strip().lower()
        self._refresh_logs()

    @Slot(object)
    def _on_log_added(self, entry: dict) -> None:
        if not self.isVisible():
            return
        line = self._format_entry(entry)
        if self._filter_text and self._filter_text not in line.lower():
            return
        self._log_display.append(line)
        self._update_count()

    def _refresh_logs(self) -> None:
        self._log_display.clear()
        lines = []
        for entry in self.state.logs:
            formatted = self._format_entry(entry.__dict__ if hasattr(entry, "__dict__") else entry)
            if self._filter_text and self._filter_text not in formatted.lower():
                continue
            lines.append(formatted)
        self._log_display.setHtml("<br>".join(lines))
        self._update_count()

    def _clear_logs(self) -> None:
        self._log_display.clear()
        self._status.setText("Cleared")

    def _update_count(self) -> None:
        # Count non-empty lines
        text = self._log_display.toPlainText()
        count = len([l for l in text.split("\n") if l.strip()])
        self._status.setText(f"{count} entries shown")

    @staticmethod
    def _format_entry(entry) -> str:
        if hasattr(entry, "timestamp"):
            ts = entry.timestamp
            level = entry.level
            source = entry.source
            message = entry.message
        elif isinstance(entry, dict):
            ts = entry.get("timestamp", "??:??:??")
            level = entry.get("level", "INFO")
            source = entry.get("source", "?")
            message = entry.get("message", "")
        else:
            return str(entry)

        color = "#778696"
        if level == "ERROR":
            color = "#e85d5d"
        elif level == "WARN" or level == "WARNING":
            color = "#e8b84d"
        elif source in {"Wake", "WakeListener"}:
            color = "#80c8b4"

        return (
            f'<span style="color:#556677">{ts}</span> '
            f'<span style="color:#b89b5c">[{source}]</span> '
            f'<span style="color:{color}">{message}</span>'
        )
