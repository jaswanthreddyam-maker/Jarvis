from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class ToastCard(QFrame):
    dismissed = Signal(object)

    def __init__(self, title: str, message: str, tone: str = "info", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("toastCard")
        self.setProperty("tone", tone)
        self.setMaximumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        title_label = QLabel(title)
        title_label.setObjectName("toastTitle")
        message_label = QLabel(message)
        message_label.setObjectName("toastMessage")
        message_label.setWordWrap(True)

        layout.addWidget(title_label)
        layout.addWidget(message_label)

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)

        self._fade_in = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_in.setDuration(180)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._fade_out = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_out.setDuration(220)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_out.finished.connect(self._finalize_dismiss)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fade_in.start()
        QTimer.singleShot(3600, self.dismiss)

    def dismiss(self) -> None:
        if self._fade_out.state() == QPropertyAnimation.State.Running:
            return
        self._fade_out.start()

    def _finalize_dismiss(self) -> None:
        self.dismissed.emit(self)
        self.deleteLater()


class ToastManager(QWidget):
    _show_signal = Signal(str, str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._show_signal.connect(self.show_toast, Qt.ConnectionType.QueuedConnection)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 18, 18)
        self._layout.setSpacing(10)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)

    def show_toast(self, title: str, message: str, tone: str = "info") -> None:
        from PySide6.QtCore import QThread
        if QThread.currentThread() != self.thread():
            self._show_signal.emit(title, message, tone)
            return
            
        toast = ToastCard(title, message, tone, self)
        toast.dismissed.connect(self._remove_toast)
        self._layout.addWidget(toast, 0, Qt.AlignmentFlag.AlignRight)
        toast.show()

    def _remove_toast(self, toast: ToastCard) -> None:
        self._layout.removeWidget(toast)
