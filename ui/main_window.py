from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Slot
from PySide6.QtGui import QAction, QColor, QCloseEvent, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QSplitter,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ui.backend_bridge import JarvisBackendBridge
from ui.overlay_window import JarvisOverlay
from ui.panels.controls_panel import ControlsPanel
from ui.panels.intelligence_panel import IntelligencePanel
from ui.panels.logs_panel import LogsPanel
from ui.panels.metrics_panel import MetricsPanel
from ui.panels.settings_panel import SettingsPanel
from ui.panels.transcript_panel import TranscriptPanel
from ui.panels.logs_viewer import LogsViewerDialog
from ui.state import AppState, Status
from jarvis.event_bus import bus, Events
from ui.widgets.status_badge import StatusBadge
from ui.widgets.toast import ToastManager


_TRAY_ICON_COLORS = {
    "idle": "#b89b5c",       # gold
    "listening": "#4CAF50",  # green
    "speaking": "#50c8b4",   # cyan
    "thinking": "#a064dc",   # purple
    "error": "#e05252",      # red
    "offline": "#555555",    # grey
}

_UI_VERBOSE = bool(os.environ.get("JARVIS_DEBUG") or os.environ.get("JARVIS_VERBOSE_UI"))


def create_app_icon(size: int = 64, ring_color: str = "#b89b5c") -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#12171d"))
    painter.drawEllipse(2, 2, size - 4, size - 4)
    painter.setBrush(QColor(ring_color))
    painter.drawEllipse(8, 8, size - 16, size - 16)
    painter.setBrush(QColor("#0e1218"))
    painter.drawEllipse(14, 14, size - 28, size - 28)
    painter.setPen(QPen(QColor("#f4ecdf"), max(2, size // 16)))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "J")
    painter.end()
    return QIcon(pixmap)


def _tone_for_status(status: str) -> str:
    lowered = status.lower()
    if status == Status.LISTENING:
        return "success"
    if status in {Status.RECOGNIZING, Status.THINKING, Status.PROCESSING, Status.INTERRUPTED}:
        return "warning"
    if status in {Status.EXECUTING, Status.RESPONDING, Status.SPEAKING}:
        return "accent"
    if status == Status.TYPING:
        return "muted"
    if status == Status.ERROR:
        return "danger"
    if "online" in lowered or "ready" in lowered:
        return "success"
    if "loading" in lowered:
        return "warning"
    if "offline" in lowered or "error" in lowered:
        return "danger"
    return "muted"


class JarvisMainWindow(QMainWindow):
    def __init__(self, state: AppState, bridge: JarvisBackendBridge) -> None:
        super().__init__()
        self.state = state
        self.bridge = bridge
        self._hero_collapsed = False
        self._safe_mode_active = False

        self.setWindowTitle("Jarvis")
        self.setWindowIcon(create_app_icon())
        self.resize(1380, 860)
        self.setMinimumSize(960, 620)

        central = QWidget()
        central.setObjectName("rootWindow")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        self.slim_status = QLabel("UI: Ready | Backend: Offline | ASR: Standby | TTS: Standby")
        self.slim_status.setStyleSheet("color: #778696; font-size: 10px; font-weight: 500;")
        self.slim_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.slim_status)

        top_bar = self._build_top_bar()
        root.addWidget(top_bar)

        self.backend_banner = QLabel("BACKEND OFFLINE: Start the Jarvis API service before using the desktop client.")
        self.backend_banner.setStyleSheet("background-color: #b89b5c; color: #12171d; font-weight: bold; padding: 6px; border-radius: 4px;")
        self.backend_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.backend_banner.hide()
        root.addWidget(self.backend_banner)

        self.hero = self._build_hero()
        root.addWidget(self.hero)

        self.body = self._build_body()
        root.addWidget(self.body, 1)

        self._body_opacity = QGraphicsOpacityEffect(self.body)
        self.body.setGraphicsEffect(self._body_opacity)
        self._body_fade = QPropertyAnimation(self._body_opacity, b"opacity", self)
        self._body_fade.setDuration(420)
        self._body_fade.setStartValue(0.0)
        self._body_fade.setEndValue(1.0)
        self._body_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._body_fade.start()

        self.hero_animation = QPropertyAnimation(self.hero, b"maximumHeight", self)
        self.hero_animation.setDuration(520)
        self.hero_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)

        self.toast_manager = ToastManager(self.centralWidget())
        self.toast_manager.raise_()

        # Desktop overlay (separate top-level window)
        self.overlay = JarvisOverlay()
        self.overlay.register_hotkey()

        self._setup_tray()
        self._wire_state()

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)

        title = QLabel("JARVIS")
        title.setObjectName("windowTitle")
        subtitle = QLabel("Desktop voice cockpit")
        subtitle.setObjectName("windowSubtitle")

        title_column = QVBoxLayout()
        title_column.setContentsMargins(0, 0, 0, 0)
        title_column.setSpacing(2)
        title_column.addWidget(title)
        title_column.addWidget(subtitle)

        self.status_badge = StatusBadge(Status.IDLE, "muted")
        self.backend_badge = StatusBadge("Backend Offline", "danger")
        self.model_badge = StatusBadge("Whisper Loading", "warning")
        self.safety_badge = StatusBadge("SAFE", "success")

        layout.addLayout(title_column)
        layout.addStretch()
        layout.addWidget(self.backend_badge)
        layout.addWidget(self.model_badge)
        layout.addWidget(self.safety_badge)
        layout.addWidget(self.status_badge)
        return bar

    def _build_hero(self) -> QWidget:
        hero = QFrame()
        hero.setObjectName("heroPanel")
        hero.setMaximumHeight(170)
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        eyebrow = QLabel("LOCAL-FIRST DESKTOP CORE")
        eyebrow.setObjectName("sectionEyebrow")
        title = QLabel("Premium voice control, built for stable daily use.")
        title.setObjectName("heroTitle")
        subtitle = QLabel(
            "Jarvis keeps the UI responsive, routes heavy work through dedicated workers, and exposes runtime status without burying the operator in noise."
        )
        subtitle.setObjectName("heroSubtitle")
        subtitle.setWordWrap(True)

        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return hero

    def _build_body(self) -> QWidget:
        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        self.controls_panel = ControlsPanel(self.state)
        self.transcript_panel = TranscriptPanel(self.state)

        right_column = QWidget()
        right_layout = QVBoxLayout(right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(14)

        self.metrics_panel = MetricsPanel(self.state)
        self.intelligence_panel = IntelligencePanel(self.state)
        self.settings_panel = SettingsPanel(self.state)
        self.logs_panel = LogsPanel(self.state)

        right_layout.addWidget(self.metrics_panel)
        right_layout.addWidget(self.intelligence_panel)
        right_layout.addWidget(self.settings_panel)
        right_layout.addWidget(self.logs_panel, 1)

        splitter.addWidget(self.controls_panel)
        splitter.addWidget(self.transcript_panel)
        splitter.addWidget(right_column)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 3)
        splitter.setSizes([280, 560, 360])

        layout.addWidget(splitter, 1)
        return body

    def _wire_state(self) -> None:
        self.controls_panel.start_requested.connect(self.bridge.start_listening)
        self.controls_panel.stop_requested.connect(self.bridge.stop_listening)
        self.controls_panel.interrupt_requested.connect(self.bridge.interrupt)

        self.settings_panel.refresh_requested.connect(self.bridge.refresh_devices)
        self.settings_panel.test_sound_requested.connect(self.bridge.test_sound)
        self.settings_panel.input_device_selected.connect(self.bridge.set_input_device)
        self.settings_panel.output_device_selected.connect(self.bridge.set_output_device)
        self.settings_panel.safe_mode_toggled.connect(self.bridge.set_safe_mode)
        self.settings_panel.safe_mode_toggled.connect(self._sync_safe_mode_label)

        self.state.status_changed.connect(self._on_status_changed)
        self.state.backend_online_changed.connect(self._on_backend_online_changed)
        self.state.model_status_changed.connect(self._on_model_status_changed)
        self.state.safety_feedback_changed.connect(self._on_safety_feedback_changed)
        self.state.toast_requested.connect(self.toast_manager.show_toast)

        # Feed overlay with live data
        self.state.mic_level_changed.connect(self.overlay.set_mic_level)
        self.state.status_changed.connect(self.overlay.set_status)
        self.state.perf_mode_changed.connect(self.overlay.set_low_perf)
        self.bridge.safe_mode_changed.connect(self.settings_panel.set_safe_mode)
        self.bridge.safe_mode_changed.connect(self._sync_safe_mode_label)

        # Intelligence panel signals
        self.intelligence_panel.clear_memory_requested.connect(self._on_clear_memory)
        self.intelligence_panel.enroll_voice_requested.connect(self._on_enroll_voice)

        # Listen for system-level notifications
        bus.subscribe("ui.notify", self._on_ui_notify)
        bus.subscribe("system.recovery_requested", self._on_recovery_notification)

        # Overlay interactive controls → bridge
        self.overlay.interrupt_requested.connect(self.bridge.interrupt)
        self.overlay.cancel_requested.connect(self.bridge.cancel_active)
        self.overlay.start_requested.connect(self.bridge.start_listening)
        self.overlay.text_submitted.connect(self.bridge.submit_text)
        self.bridge.text_response_ready.connect(self.overlay.show_response)

        # Wake word UI trigger
        self.bridge.wake_detected.connect(self._on_wake_detected)

        self._on_status_changed(self.state.status)
        self._on_backend_online_changed(self.state.backend_online)
        self._on_model_status_changed(self.state.model_status)
        self._on_safety_feedback_changed(self.state.safety_feedback)

    def _setup_tray(self) -> None:
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(create_app_icon())
        self.tray_icon.setToolTip("Jarvis: Idle")
        self._wake_enabled = bool(getattr(self.bridge, "wake_listener_enabled", False))
        self._tray_icon_cache: dict[str, QIcon] = {}

        menu = QMenu(self)

        show_action = QAction("Open Jarvis", self)
        show_action.triggered.connect(self._restore_from_tray)

        overlay_action = QAction("Toggle Overlay", self)
        overlay_action.triggered.connect(self.overlay.toggle)

        menu.addAction(show_action)
        menu.addAction(overlay_action)
        menu.addSeparator()

        start_action = QAction("Start Listening", self)
        start_action.triggered.connect(self.bridge.start_listening)
        stop_action = QAction("Stop Listening", self)
        stop_action.triggered.connect(self.bridge.stop_listening)
        menu.addAction(start_action)
        menu.addAction(stop_action)

        self._wake_toggle_action = QAction("Wake Listening", self)
        self._wake_toggle_action.triggered.connect(self._toggle_wake_listening)
        self._wake_toggle_action.setText(
            "Wake Listening: On" if self._wake_enabled else "Wake Listening: Off"
        )
        menu.addAction(self._wake_toggle_action)
        menu.addSeparator()

        logs_action = QAction("View Logs", self)
        logs_action.triggered.connect(self._show_logs)
        menu.addAction(logs_action)

        reload_action = QAction("Reload Config", self)
        reload_action.triggered.connect(self._reload_config)
        menu.addAction(reload_action)
        menu.addSeparator()
        
        self._safe_mode_tray_action = QAction("Enable Safe Mode", self)
        self._safe_mode_tray_action.triggered.connect(self._toggle_safe_mode_from_tray)
        menu.addAction(self._safe_mode_tray_action)
        menu.addSeparator()

        restart_action = QAction("Restart", self)
        restart_action.triggered.connect(self._restart_app)
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(restart_action)
        menu.addAction(quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _get_tray_icon(self, color_key: str) -> QIcon:
        if color_key not in self._tray_icon_cache:
            color = _TRAY_ICON_COLORS.get(color_key, "#b89b5c")
            self._tray_icon_cache[color_key] = create_app_icon(64, color)
        return self._tray_icon_cache[color_key]

    def _update_tray_state(self, status: str) -> None:
        lowered = status.lower()
        if lowered in {"listening", "recognizing"}:
            icon_key = "listening"
            tooltip = "Jarvis: Listening"
        elif lowered == "typing":
            icon_key = "idle"
            tooltip = "Jarvis: Ready"
        elif lowered in {"executing", "responding", "speaking"}:
            icon_key = "speaking"
            tooltip = "Jarvis: Responding"
        elif lowered in {"thinking", "processing"}:
            icon_key = "thinking"
            tooltip = "Jarvis: Processing"
        elif lowered == "error":
            icon_key = "error"
            tooltip = "Jarvis: Error"
        else:
            icon_key = "idle"
            tooltip = "Jarvis: Idle"

        self.tray_icon.setIcon(self._get_tray_icon(icon_key))
        self.tray_icon.setToolTip(tooltip)

    def _toggle_wake_listening(self) -> None:
        self._wake_enabled = not self._wake_enabled
        if self._wake_enabled:
            self.bridge.start_listening_continuous()
            self.tray_icon.showMessage(
                "Jarvis", 'Wake word "Hey Jarvis" is ON.',
                QSystemTrayIcon.MessageIcon.Information, 1500,
            )
            self._wake_toggle_action.setText("Wake Listening: On")
        else:
            self.bridge.stop_listening_continuous()
            self.tray_icon.showMessage(
                "Jarvis", "Wake word listening paused.",
                QSystemTrayIcon.MessageIcon.Information, 1500,
            )
            self._wake_toggle_action.setText("Wake Listening: Off")

    def _show_logs(self) -> None:
        if not hasattr(self, "_logs_dialog"):
            self._logs_dialog = LogsViewerDialog(self.state, self)
        self._logs_dialog.showNormal()
        self._logs_dialog.raise_()
        self._logs_dialog.activateWindow()

    def _on_status_changed(self, status: str) -> None:
        self.status_badge.set_text(status, _tone_for_status(status))
        self._update_tray_state(status)
        if status == Status.IDLE:
            if self._hero_collapsed:
                self._expand_hero()
        elif not self._hero_collapsed:
            self._collapse_hero()

    def _update_slim_status(self) -> None:
        backend = self.state.model_status.get("backend", "Offline")
        asr = self.state.model_status.get("asr", "Unknown")
        tts = self.state.model_status.get("tts", "Unknown")
        safety = self.state.safety_feedback.get("safety_level", "SAFE")
        self.slim_status.setText(f"UI: Ready | Backend: {backend} | ASR: {asr} | TTS: {tts} | Safety: {safety}")

    def _on_backend_online_changed(self, online: bool) -> None:
        text = "Backend Online" if online else "Backend Offline"
        self.backend_badge.set_text(text, _tone_for_status(text))
        self.backend_banner.setVisible(not online)
        self._update_slim_status()

    def _on_model_status_changed(self, model_status: dict[str, str]) -> None:
        asr_text = f"Whisper {model_status.get('asr', 'Unknown')}"
        self.model_badge.set_text(asr_text, _tone_for_status(asr_text))
        self.backend_banner.setVisible(not self.state.backend_online)
        self._update_slim_status()

    def _on_safety_feedback_changed(self, payload: dict[str, str]) -> None:
        safety_level = payload.get("safety_level", "SAFE") or "SAFE"
        tone = {
            "SAFE": "success",
            "CONFIRMATION REQUIRED": "warning",
            "BLOCKED": "danger",
        }.get(safety_level, "muted")
        self.safety_badge.set_text(safety_level, tone)
        self._update_slim_status()

    def _on_ui_notify(self, title: str, message: str) -> None:
        from PySide6.QtCore import QMetaObject, Q_ARG, Qt
        # Ensure thread safety for UI update
        QMetaObject.invokeMethod(self, "_show_tray_message", Qt.ConnectionType.QueuedConnection, Q_ARG(str, title), Q_ARG(str, message))

    @Slot(str, str)
    def _show_tray_message(self, title: str, message: str) -> None:
        self.tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Warning, 3000)

    def _on_recovery_notification(self, restart: bool = True, target: str = "all") -> None:
        if target != "all":
            self._on_ui_notify("Jarvis Recovery", f"Restarting module: {target}")
        else:
            self._on_ui_notify("Jarvis Recovery", "Initiating full system restart...")

    def _collapse_hero(self) -> None:
        self._hero_collapsed = True
        self.hero_animation.stop()
        self.hero_animation.setStartValue(self.hero.maximumHeight())
        self.hero_animation.setEndValue(0)
        self.hero_animation.start()

    def _expand_hero(self) -> None:
        self._hero_collapsed = False
        self.hero_animation.stop()
        self.hero_animation.setStartValue(self.hero.maximumHeight())
        self.hero_animation.setEndValue(170)
        self.hero_animation.start()

    def _toggle_safe_mode_from_tray(self) -> None:
        self.bridge.set_safe_mode(not self._safe_mode_active)

    def _sync_safe_mode_label(self, enabled: bool) -> None:
        self._safe_mode_active = enabled
        label = "Disable Safe Mode" if enabled else "Enable Safe Mode"
        self._safe_mode_tray_action.setText(label)

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self.tray_icon.hide()
        if hasattr(self, "lifecycle"):
            self.lifecycle.shutdown(restart=False)
        else:
            self.overlay.unregister_hotkey()
            self.overlay.hide_overlay()
            self.bridge.stop_listening_continuous()
            self.bridge.shutdown()
            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()

    def _restart_app(self) -> None:
        """Restart the application safely via lifecycle."""
        self.tray_icon.hide()
        if hasattr(self, "lifecycle"):
            self.lifecycle.shutdown(restart=True)
        else:
            self._quit_from_tray()
            import subprocess
            if getattr(sys, "frozen", False):
                subprocess.Popen([sys.executable] + sys.argv[1:])
            else:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "app.py")] + sys.argv[1:])

    def _reload_config(self) -> None:
        """Publish an event to trigger a hot-reload of the configuration."""
        bus.publish(Events.CONFIG_RELOADED)
        self.tray_icon.showMessage(
            "Jarvis", "Configuration reloaded successfully.",
            QSystemTrayIcon.MessageIcon.Information, 1500,
        )

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            # Left-click → toggle overlay (quick wake)
            self.overlay.toggle()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            # Double-click → open cockpit window
            self._restore_from_tray()

    def _on_wake_detected(self) -> None:
        self.overlay.set_status("LISTENING")
        if not self.overlay._active:
            self.overlay.show_overlay()

    def _on_clear_memory(self) -> None:
        """Clear the memory graph (privacy action)."""
        try:
            from jarvis.memory_graph import graph
            graph.clear_all()
            self.state.push_toast("Memory Cleared", "All learned patterns have been wiped.", "info")
            self.state.add_log("Memory graph cleared by user.", source="Intelligence")
        except Exception as e:
            self.state.push_toast("Error", f"Failed to clear memory: {e}", "error")

    def _on_enroll_voice(self) -> None:
        """Start voice enrollment process."""
        self.state.push_toast(
            "Voice Enrollment",
            "Say 3 sentences to enroll your voice profile. Recording starts now.",
            "info",
        )
        self.state.add_log("Voice enrollment started.", source="Intelligence")
        # Trigger enrollment via bridge
        try:
            from jarvis.voice_identity import voice_id
            if voice_id.is_enrolled:
                voice_id.clear_enrollment()
            # The actual enrollment happens when audio is captured by the wake listener.
            # We set a flag that the next 3 utterances should be used for enrollment.
            self.bridge._enrollment_mode = True
            self.bridge._enrollment_count = 0
        except Exception as e:
            self.state.push_toast("Error", f"Enrollment failed: {e}", "error")

    def start_welcome_sequence(self) -> None:
        if _UI_VERBOSE:
            print("[UI] Showing welcome overlay", flush=True)
        
        self.overlay.unregister_hotkey()
        self.overlay.set_status("Jarvis is online")
        
        # Fallback to ensure window shows
        def _ensure_shown():
            if not self.overlay._active:
                self.overlay.show_overlay()
            self.overlay._set_click_through(False)
            
        self.overlay.show_overlay()
        self.overlay._set_click_through(False)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(150, _ensure_shown)
        
        self.bridge.welcome_tts_requested.emit()
        QTimer.singleShot(2500, self._finish_welcome_sequence)
        
    def _finish_welcome_sequence(self) -> None:
        if _UI_VERBOSE:
            print("[UI] Welcome complete -> entering idle mode", flush=True)
        self.overlay.set_status("Ready")
        
        def _hide():
            self.overlay.set_status("IDLE")
            self.overlay.hide_overlay()
            self.overlay.register_hotkey()
            
        from PySide6.QtCore import QTimer
        QTimer.singleShot(400, _hide)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.toast_manager is not None:
            self.toast_manager.setGeometry(self.centralWidget().rect())

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tray_icon.isVisible():
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "Jarvis",
                "Jarvis is still running in the system tray.",
                QSystemTrayIcon.MessageIcon.Information,
                1800,
            )
            return
        super().closeEvent(event)
