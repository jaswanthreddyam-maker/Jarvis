"""Intelligence Panel — displays proactive suggestions, voice identity status,
and memory graph controls.

Non-intrusive: suggestions appear as dismissible cards.
User can accept/reject predictions and manage their voice profile.
"""
from __future__ import annotations

from PySide6.QtCore import Signal, Slot, QTimer, Qt
from PySide6.QtWidgets import (
    QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QWidget, QScrollArea, QProgressBar, QSizePolicy,
)
import logging

from ui.state import AppState

logger = logging.getLogger("Jarvis.IntelligencePanel")


class SuggestionCard(QFrame):
    """A single proactive suggestion with accept/reject controls."""

    accepted = Signal(str)   # action_id
    rejected = Signal(str)   # action_id

    def __init__(self, action_id: str, label: str, reason: str,
                 confidence: float, parent=None) -> None:
        super().__init__(parent)
        self._action_id = action_id
        self.setObjectName("suggestionCard")
        self.setStyleSheet("""
            #suggestionCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(184, 155, 92, 0.08), stop:1 rgba(80, 200, 180, 0.06));
                border: 1px solid rgba(184, 155, 92, 0.25);
                border-radius: 8px;
                padding: 12px;
            }
            #suggestionCard:hover {
                border-color: rgba(184, 155, 92, 0.5);
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # Header row
        header = QHBoxLayout()
        icon = QLabel("💡")
        icon.setStyleSheet("font-size: 16px;")
        title = QLabel(label)
        title.setStyleSheet("color: #f4ecdf; font-weight: 600; font-size: 13px;")
        conf_label = QLabel(f"{confidence:.0%}")
        conf_label.setStyleSheet("color: #50c8b4; font-size: 11px; font-weight: 500;")
        header.addWidget(icon)
        header.addWidget(title, 1)
        header.addWidget(conf_label)
        layout.addLayout(header)

        # Reason
        reason_label = QLabel(reason)
        reason_label.setStyleSheet("color: #778696; font-size: 11px;")
        reason_label.setWordWrap(True)
        layout.addWidget(reason_label)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        accept_btn = QPushButton("✓ Accept")
        accept_btn.setObjectName("accentButton")
        accept_btn.setStyleSheet("""
            QPushButton {
                background: rgba(80, 200, 180, 0.15);
                color: #50c8b4;
                border: 1px solid rgba(80, 200, 180, 0.3);
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover { background: rgba(80, 200, 180, 0.25); }
        """)
        accept_btn.clicked.connect(lambda: self.accepted.emit(self._action_id))

        dismiss_btn = QPushButton("✗ Dismiss")
        dismiss_btn.setStyleSheet("""
            QPushButton {
                background: rgba(119, 134, 150, 0.1);
                color: #778696;
                border: 1px solid rgba(119, 134, 150, 0.2);
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 11px;
            }
            QPushButton:hover { background: rgba(119, 134, 150, 0.2); }
        """)
        dismiss_btn.clicked.connect(lambda: self.rejected.emit(self._action_id))

        btn_row.addStretch()
        btn_row.addWidget(accept_btn)
        btn_row.addWidget(dismiss_btn)
        layout.addLayout(btn_row)


class IntelligencePanel(QFrame):
    """Main intelligence panel showing proactive suggestions,
    voice identity status, and memory controls."""

    suggestion_accepted = Signal(str)
    suggestion_rejected = Signal(str)
    clear_memory_requested = Signal()
    enroll_voice_requested = Signal()

    def __init__(self, state: AppState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self._bus_subscribed = False
        self.setObjectName("panel")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        # ── Section: Intelligence Status ─────────────────────────────
        eyebrow = QLabel("INTELLIGENCE")
        eyebrow.setObjectName("sectionEyebrow")
        root.addWidget(eyebrow)

        title = QLabel("Proactive brain")
        title.setObjectName("panelTitle")
        root.addWidget(title)

        # Voice Identity indicator
        self._voice_row = QHBoxLayout()
        self._voice_icon = QLabel("🔒")
        self._voice_icon.setStyleSheet("font-size: 14px;")
        self._voice_label = QLabel("Voice: Not enrolled")
        self._voice_label.setStyleSheet("color: #778696; font-size: 11px;")
        self._voice_row.addWidget(self._voice_icon)
        self._voice_row.addWidget(self._voice_label, 1)

        self._enroll_btn = QPushButton("Enroll Voice")
        self._enroll_btn.setObjectName("secondaryButton")
        self._enroll_btn.setStyleSheet("""
            QPushButton {
                background: rgba(184, 155, 92, 0.12);
                color: #b89b5c;
                border: 1px solid rgba(184, 155, 92, 0.3);
                border-radius: 4px;
                padding: 4px 10px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover { background: rgba(184, 155, 92, 0.22); }
        """)
        self._enroll_btn.clicked.connect(self.enroll_voice_requested.emit)
        self._voice_row.addWidget(self._enroll_btn)
        root.addLayout(self._voice_row)

        # Memory stats
        self._memory_row = QHBoxLayout()
        self._memory_icon = QLabel("🧠")
        self._memory_icon.setStyleSheet("font-size: 14px;")
        self._memory_label = QLabel("Memory: 0 nodes, 0 edges")
        self._memory_label.setStyleSheet("color: #778696; font-size: 11px;")
        self._memory_row.addWidget(self._memory_icon)
        self._memory_row.addWidget(self._memory_label, 1)

        self._clear_memory_btn = QPushButton("Clear Memory")
        self._clear_memory_btn.setStyleSheet("""
            QPushButton {
                background: rgba(224, 82, 82, 0.1);
                color: #e05252;
                border: 1px solid rgba(224, 82, 82, 0.25);
                border-radius: 4px;
                padding: 4px 10px;
                font-size: 11px;
            }
            QPushButton:hover { background: rgba(224, 82, 82, 0.2); }
        """)
        self._clear_memory_btn.clicked.connect(self.clear_memory_requested.emit)
        self._memory_row.addWidget(self._clear_memory_btn)
        root.addLayout(self._memory_row)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: rgba(119, 134, 150, 0.15);")
        root.addWidget(sep)

        # ── Section: Suggestions ─────────────────────────────────────
        suggestions_label = QLabel("SUGGESTIONS")
        suggestions_label.setObjectName("sectionEyebrow")
        root.addWidget(suggestions_label)

        # Scrollable suggestion area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("background: transparent;")

        self._suggestions_container = QWidget()
        self._suggestions_layout = QVBoxLayout(self._suggestions_container)
        self._suggestions_layout.setContentsMargins(0, 0, 0, 0)
        self._suggestions_layout.setSpacing(8)
        self._suggestions_layout.addStretch()

        self._no_suggestions = QLabel("No suggestions yet. Keep using Jarvis to build patterns.")
        self._no_suggestions.setStyleSheet("color: #555; font-size: 11px; padding: 8px;")
        self._no_suggestions.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._suggestions_layout.insertWidget(0, self._no_suggestions)

        self._scroll.setWidget(self._suggestions_container)
        root.addWidget(self._scroll, 1)

        # ── Periodic refresh ─────────────────────────────────────────
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_data)
        self._refresh_timer.start(15000)  # Refresh every 15 seconds

        # Subscribe to proactive events
        self._subscribe_bus()
        self.destroyed.connect(self._unsubscribe_bus)

        # Initial refresh
        QTimer.singleShot(2000, self._refresh_data)

    # ── Data refresh ─────────────────────────────────────────────────

    @Slot()
    def _refresh_data(self) -> None:
        """Refresh voice identity and memory stats."""
        # Voice identity
        try:
            from jarvis.voice_identity import voice_id
            if voice_id.is_enrolled:
                mode = voice_id.current_mode
                if mode == voice_id.Mode.AUTHENTICATED:
                    self._voice_icon.setText("🔓")
                    self._voice_label.setText("Voice: Authenticated")
                    self._voice_label.setStyleSheet("color: #50c8b4; font-size: 11px;")
                    self._enroll_btn.setText("Re-enroll")
                elif mode == voice_id.Mode.GUEST:
                    self._voice_icon.setText("🔒")
                    self._voice_label.setText("Voice: Guest mode")
                    self._voice_label.setStyleSheet("color: #e05252; font-size: 11px;")
                    self._enroll_btn.setText("Re-enroll")
                else:
                    self._voice_icon.setText("🔑")
                    self._voice_label.setText("Voice: Enrolled (pending verification)")
                    self._voice_label.setStyleSheet("color: #b89b5c; font-size: 11px;")
                    self._enroll_btn.setText("Re-enroll")
            else:
                self._voice_icon.setText("🔒")
                self._voice_label.setText("Voice: Not enrolled")
                self._voice_label.setStyleSheet("color: #778696; font-size: 11px;")
                self._enroll_btn.setText("Enroll Voice")
        except Exception as e:
            logger.error("Failed to refresh voice identity data: %s", e)

        # Memory graph stats
        try:
            from jarvis.memory_graph import graph
            stats = graph.stats()
            nodes = stats.get("total_nodes", 0)
            edges = stats.get("total_edges", 0)
            types = stats.get("node_types", {})
            apps = types.get("app", 0)
            actions = types.get("action", 0)
            self._memory_label.setText(
                f"Memory: {nodes} nodes, {edges} edges ({apps} apps, {actions} actions)")
        except Exception as e:
            logger.error("Failed to refresh memory graph stats: %s", e)

    def _subscribe_bus(self) -> None:
        if self._bus_subscribed:
            return
        try:
            from jarvis.event_bus import bus

            bus.subscribe("proactive.suggestion", self._on_suggestion)
            bus.subscribe("proactive.prediction_ready", self._on_prediction)
            self._bus_subscribed = True
        except Exception as e:
            logger.error("Failed to subscribe to proactive event bus: %s", e)

    def _unsubscribe_bus(self, *_args) -> None:
        if not self._bus_subscribed:
            return
        try:
            from jarvis.event_bus import bus

            bus.unsubscribe("proactive.suggestion", self._on_suggestion)
            bus.unsubscribe("proactive.prediction_ready", self._on_prediction)
        except Exception as e:
            logger.error("Failed to unsubscribe from proactive event bus: %s", e)
        finally:
            self._bus_subscribed = False

    # ── Suggestion handling ──────────────────────────────────────────

    def _on_suggestion(self, data: dict) -> None:
        """Handle proactive suggestion from the engine."""
        from PySide6.QtCore import QMetaObject, Qt
        # Thread-safe UI update
        QMetaObject.invokeMethod(
            self, "_add_suggestion_card",
            Qt.ConnectionType.QueuedConnection,
        )

    def _on_prediction(self, data: dict) -> None:
        """Handle prediction ready event."""
        self._on_suggestion(data)

    @Slot()
    def _add_suggestion_card(self) -> None:
        """Pull predictions and display as cards."""
        try:
            from jarvis.predictive_engine import predictor
            predictions = predictor.get_predictions()
        except Exception as e:
            logger.error("Failed to retrieve predictions: %s", e)
            return

        # Clear existing cards
        self._clear_suggestion_cards()

        if not predictions:
            self._no_suggestions.show()
            return

        self._no_suggestions.hide()
        for pred in predictions[:3]:
            card = SuggestionCard(
                action_id=pred.action_id,
                label=pred.label,
                reason=pred.reason,
                confidence=pred.confidence,
                parent=self._suggestions_container,
            )
            card.accepted.connect(self._on_card_accepted)
            card.rejected.connect(self._on_card_rejected)
            # Insert before the stretch
            idx = self._suggestions_layout.count() - 1
            self._suggestions_layout.insertWidget(idx, card)

    def _clear_suggestion_cards(self) -> None:
        """Remove all existing suggestion cards."""
        for i in reversed(range(self._suggestions_layout.count())):
            item = self._suggestions_layout.itemAt(i)
            widget = item.widget() if item else None
            if widget and isinstance(widget, SuggestionCard):
                widget.deleteLater()

    @Slot(str)
    def _on_card_accepted(self, action_id: str) -> None:
        self.suggestion_accepted.emit(action_id)
        try:
            from jarvis.proactive_engine import proactive
            proactive.accept_suggestion(action_id)
        except Exception as e:
            logger.error("Failed to accept suggestion %s: %s", action_id, e)
        self._state.push_toast("Suggestion Accepted", f"Acting on: {action_id}", "info")
        self._add_suggestion_card()  # Refresh

    @Slot(str)
    def _on_card_rejected(self, action_id: str) -> None:
        self.suggestion_rejected.emit(action_id)
        try:
            from jarvis.proactive_engine import proactive
            proactive.reject_suggestion(action_id)
        except Exception as e:
            logger.error("Failed to reject suggestion %s: %s", action_id, e)
        self._add_suggestion_card()  # Refresh
