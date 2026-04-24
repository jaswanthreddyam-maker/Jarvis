from __future__ import annotations

import os
from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCursor, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QGraphicsOpacityEffect,
    QApplication,
)

class ConfirmationOverlay(QWidget):
    """
    A non-blocking, always-on-top floating overlay for action confirmations.
    """
    confirmed = Signal(str)  # confirmation_id
    cancelled = Signal(str)  # confirmation_id

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._confirmation_id: str | None = None
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)

        self._setup_ui()
        self._setup_animations()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        self.card = QFrame()
        self.card.setObjectName("confirmationCard")
        # Base styling for the card
        self.card.setStyleSheet(
            """
            #confirmationCard {
                background-color: rgba(18, 23, 29, 240);
                border-radius: 12px;
                border: 1px solid rgba(184, 155, 92, 0.3);
            }
            QLabel {
                color: #f4ecdf;
            }
            """
        )

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)

        # Header with icon and title
        header_layout = QHBoxLayout()
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(24, 24)
        
        self.title_label = QLabel("Confirmation Required")
        self.title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        
        header_layout.addWidget(self.icon_label)
        header_layout.addWidget(self.title_label)
        header_layout.addStretch()

        # Message
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.message_label.setStyleSheet("font-size: 13px; color: rgba(244, 236, 223, 0.8);")

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.addStretch()

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_cancel.clicked.connect(self._on_cancel)
        
        self.btn_confirm = QPushButton("Confirm")
        self.btn_confirm.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_confirm.clicked.connect(self._on_confirm)

        self._style_buttons("uncertain")  # default

        button_layout.addWidget(self.btn_cancel)
        button_layout.addWidget(self.btn_confirm)

        card_layout.addLayout(header_layout)
        card_layout.addWidget(self.message_label)
        card_layout.addLayout(button_layout)

        layout.addWidget(self.card)
        self.setFixedSize(360, 160)

    def _setup_animations(self) -> None:
        self._opacity = QGraphicsOpacityEffect(self.card)
        self.card.setGraphicsEffect(self._opacity)

        self._fade_in = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_in.setDuration(250)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._fade_out = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_out.setDuration(200)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_out.finished.connect(self.hide)

    def _style_buttons(self, c_type: str) -> None:
        base_btn = """
            QPushButton {
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: 600;
                font-size: 12px;
            }
        """
        
        cancel_style = base_btn + """
            QPushButton {
                background: transparent;
                border: 1px solid rgba(244, 236, 223, 0.2);
                color: #f4ecdf;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.05); }
        """
        self.btn_cancel.setStyleSheet(cancel_style)

        if c_type == "destructive":
            confirm_style = base_btn + """
                QPushButton {
                    background: rgba(224, 82, 82, 0.2);
                    border: 1px solid #e05252;
                    color: #ffcccc;
                }
                QPushButton:hover { background: rgba(224, 82, 82, 0.35); }
            """
            self.title_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #e05252;")
            self.icon_label.setText("🟥")
        elif c_type == "external":
            confirm_style = base_btn + """
                QPushButton {
                    background: rgba(184, 155, 92, 0.2);
                    border: 1px solid #b89b5c;
                    color: #f4ecdf;
                }
                QPushButton:hover { background: rgba(184, 155, 92, 0.35); }
            """
            self.title_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #b89b5c;")
            self.icon_label.setText("🟨")
        else: # uncertain
            confirm_style = base_btn + """
                QPushButton {
                    background: rgba(80, 200, 180, 0.2);
                    border: 1px solid #50c8b4;
                    color: #e0ffff;
                }
                QPushButton:hover { background: rgba(80, 200, 180, 0.35); }
            """
            self.title_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #50c8b4;")
            self.icon_label.setText("🟦")

        self.btn_confirm.setStyleSheet(confirm_style)
        
        # Also adjust card border
        border_colors = {
            "destructive": "rgba(224, 82, 82, 0.4)",
            "external": "rgba(184, 155, 92, 0.4)",
            "uncertain": "rgba(80, 200, 180, 0.4)"
        }
        b_color = border_colors.get(c_type, border_colors["uncertain"])
        self.card.setStyleSheet(f"""
            #confirmationCard {{
                background-color: rgba(18, 23, 29, 240);
                border-radius: 12px;
                border: 1px solid {b_color};
            }}
            QLabel {{ color: #f4ecdf; }}
        """)

    @Slot(str, str, str, str)
    def show_confirmation(self, confirmation_id: str, title: str, message: str, c_type: str = "uncertain") -> None:
        if self.isVisible():
            self._fade_out.stop()

        self._confirmation_id = confirmation_id
        self.title_label.setText(title)
        self.message_label.setText(message)
        self._style_buttons(c_type)

        self._position_window()
        self.show()
        self.raise_()

        self._fade_in.start()
        
        # 30 seconds timeout
        self._timeout_timer.start(29500)

    def _position_window(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos())
        if not screen:
            screen = QGuiApplication.primaryScreen()
        if not screen:
            return
            
        # Top right positioning
        geo = screen.availableGeometry()
        x = geo.width() - self.width() - 40
        y = 40
        self.move(x, y)

    @Slot()
    def _on_confirm(self) -> None:
        self._timeout_timer.stop()
        if self._confirmation_id:
            self.confirmed.emit(self._confirmation_id)
        self._dismiss()

    @Slot()
    def _on_cancel(self) -> None:
        self._timeout_timer.stop()
        if self._confirmation_id:
            self.cancelled.emit(self._confirmation_id)
        self._dismiss()

    @Slot()
    def _on_timeout(self) -> None:
        # Just dismiss visually. The backend handles the timeout expiration logic.
        self._dismiss()

    def _dismiss(self) -> None:
        self._confirmation_id = None
        if self._fade_in.state() == QPropertyAnimation.State.Running:
            self._fade_in.stop()
        self._fade_out.start()
