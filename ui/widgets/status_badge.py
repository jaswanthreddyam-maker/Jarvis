from __future__ import annotations

from PySide6.QtWidgets import QLabel


class StatusBadge(QLabel):
    def __init__(self, text: str = "", tone: str = "muted", parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName("statusBadge")
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_text(self, text: str, tone: str | None = None) -> None:
        self.setText(text)
        if tone is not None:
            self.set_tone(tone)
