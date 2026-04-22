from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget


class MicOrbWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._display_level = 0.0
        self._status = "Idle"
        self._animation = QPropertyAnimation(self, b"displayLevel", self)
        self._animation.setDuration(120)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.setMinimumSize(220, 220)

    def get_display_level(self) -> float:
        return self._display_level

    def set_display_level(self, value: float) -> None:
        self._display_level = max(0.0, min(1.0, float(value)))
        self.update()

    displayLevel = Property(float, get_display_level, set_display_level)

    def set_level(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        self._animation.stop()
        self._animation.setStartValue(self._display_level)
        self._animation.setEndValue(value)
        self._animation.start()

    def set_status(self, status: str) -> None:
        self._status = status
        self.update()

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect().adjusted(16, 16, -16, -16)
        radius = min(rect.width(), rect.height()) / 2
        center = rect.center()

        halo_radius = radius * (0.86 + self._display_level * 0.22)
        halo = QRadialGradient(center, halo_radius)
        halo.setColorAt(0.0, QColor(212, 178, 106, 70 + int(self._display_level * 50)))
        halo.setColorAt(0.55, QColor(212, 178, 106, 24))
        halo.setColorAt(1.0, QColor(212, 178, 106, 0))
        painter.setBrush(halo)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, halo_radius, halo_radius)

        ring_pen = QPen(QColor(185, 155, 92, 120), 3)
        painter.setPen(ring_pen)
        painter.setBrush(QColor(18, 22, 28, 240))
        painter.drawEllipse(center, radius * 0.68, radius * 0.68)

        inner_gradient = QRadialGradient(center, radius * 0.58)
        inner_gradient.setColorAt(0.0, QColor(44, 52, 66))
        inner_gradient.setColorAt(0.65, QColor(22, 28, 36))
        inner_gradient.setColorAt(1.0, QColor(10, 14, 19))
        painter.setBrush(inner_gradient)
        painter.setPen(QPen(QColor(215, 190, 136, 140), 1))
        painter.drawEllipse(center, radius * 0.52, radius * 0.52)

        pulse_radius = radius * (0.22 + self._display_level * 0.18)
        painter.setBrush(QColor(216, 184, 110, 160))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, pulse_radius, pulse_radius)

        painter.setPen(QColor(242, 232, 215))
        painter.setFont(QFont("Bahnschrift SemiBold", 18))
        painter.drawText(rect.adjusted(0, -12, 0, -4), Qt.AlignmentFlag.AlignCenter, "JARVIS")

        painter.setPen(QColor(180, 188, 198))
        painter.setFont(QFont("Segoe UI Semibold", 10))
        painter.drawText(rect.adjusted(0, 22, 0, 0), Qt.AlignmentFlag.AlignCenter, self._status.upper())
