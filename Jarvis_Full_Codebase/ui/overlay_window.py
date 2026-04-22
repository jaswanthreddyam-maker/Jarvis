"""
Jarvis Desktop Overlay — full-screen transparent HUD (v2).

Ctrl+Space activates a translucent overlay with animated waveform,
pulsing orb, and interactive controls.  Click-through is toggled
dynamically: enabled while idle, disabled while the user is
interacting (speaking/thinking/speaking-back).
"""
from __future__ import annotations

import math
import random
import ctypes
import ctypes.wintypes
import os
import sys
import threading
import time
import logging
from typing import Optional
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QThread,
    QTimer,
    Property,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QKeyEvent,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from ui.input_handler import OverlayInputHandler, OverlaySessionState
from ui.widgets.chat_input import OverlayChatPanel
from ui.widgets.streaming_text import StreamingTextLabel


OVERLAY_VERBOSE = bool(os.environ.get("JARVIS_DEBUG") or os.environ.get("JARVIS_VERBOSE_OVERLAY"))
logger = logging.getLogger("Jarvis.Overlay")


def _overlay_debug(message: str) -> None:
    if OVERLAY_VERBOSE:
        print(message, flush=True)


def _overlay_notice(message: str) -> None:
    print(message, flush=True)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
_FPS_HIGH = 30
_FPS_LOW = 15
_WAVE_POINTS = 120
_PARTICLE_COUNT = 28
_ORB_BASE_RADIUS = 72
_FRAME_INTERVAL_ACTIVE_MS = max(1, round(1000 / _FPS_HIGH))
_FRAME_INTERVAL_IDLE_MS = max(1, round(1000 / _FPS_LOW))
_CONTROL_HIDE_OFFSET = 22
_CHAT_HIDE_OFFSET = 18

# Background opacity targets (alpha 0-255).  35-45% ≈ 89-115
_BG_ALPHA_CENTER = 85
_BG_ALPHA_MID = 78
_BG_ALPHA_EDGE = 100


# ---------------------------------------------------------------------------
# Global hotkey (Windows RegisterHotKey in a daemon thread)
# ---------------------------------------------------------------------------
class _WinHotkey(threading.Thread):
    """Register Ctrl+Alt+Space at the OS level and fire *callback* on press."""

    _HOTKEY_ID = 0xBF01
    _MOD_CTRL_ALT = 0x0002 | 0x0001
    _VK_SPACE = 0x20
    _WM_HOTKEY = 0x0312

    def __init__(self, callback):
        super().__init__(daemon=True)
        self._cb = callback
        self._running = True

    def run(self):
        user32 = ctypes.windll.user32
        
        registered = user32.RegisterHotKey(None, self._HOTKEY_ID, self._MOD_CTRL_ALT, self._VK_SPACE)
        if not registered:
            _overlay_notice("[Overlay] Ctrl+Alt+Space unavailable. Falling back to Ctrl+Alt+J.")
            # Fallback to Ctrl+Alt+J
            VK_J = 0x4A
            registered = user32.RegisterHotKey(None, self._HOTKEY_ID, self._MOD_CTRL_ALT, VK_J)
            if not registered:
                _overlay_notice("[Overlay] Global hotkey registration failed.")
                return

        msg = ctypes.wintypes.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            if msg.message == self._WM_HOTKEY:
                _overlay_debug("[Overlay] Hotkey pressed")
                self._cb()
        user32.UnregisterHotKey(None, self._HOTKEY_ID)

    def stop(self):
        self._running = False
        try:
            ctypes.windll.user32.PostThreadMessageW(self.ident, 0x0012, 0, 0)
        except Exception as e:
            logger.error("Failed to post stop message to hotkey thread: %s", e)

# ---------------------------------------------------------------------------
# Sound-cue helper (non-blocking)
# ---------------------------------------------------------------------------
def _play_cue(cue_name: str):
    """Play a custom wav cue via winsound, fallback to Beep if missing."""
    if sys.platform != "win32":
        return

    def _play():
        try:
            import winsound
            assets_dir = Path(__file__).resolve().parent.parent / "assets"
            wav_path = assets_dir / f"{cue_name}.wav"
            if wav_path.exists():
                winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            else:
                # Fallback
                freq = 700 if cue_name == "activate" else 400
                winsound.Beep(freq, 60)
        except Exception as e:
            logger.error("Failed to play audio cue %s: %s", cue_name, e)
    threading.Thread(target=_play, daemon=True).start()


# ---------------------------------------------------------------------------
# Overlay Widget
# ---------------------------------------------------------------------------
class JarvisOverlay(QWidget):
    """Full-screen transparent overlay with waveform + animated orb."""

    # Signals the main window can connect to
    start_requested = Signal()
    interrupt_requested = Signal()
    cancel_requested = Signal()
    text_submitted = Signal(str)       # chat text submitted by user
    
    # Internal signals for thread-safe HUD management
    _show_signal = Signal()
    _hide_signal = Signal()
    _toggle_signal = Signal()
    _status_signal = Signal(str)
    _mic_signal = Signal(float)
    _perf_signal = Signal(bool)
    _hotkey_signal = Signal()

    # -- animated property for smooth fade --------------------------------
    def _get_fade(self) -> float:
        return self._fade_value

    def _set_fade(self, v: float):
        self._fade_value = v
        self.setWindowOpacity(v)

    fadeLevel = Property(float, _get_fade, _set_fade)

    # ---------------------------------------------------------------------
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # ---- state ------------------------------------------------------
        self._active = False
        self._fade_value = 0.0
        self._mic_level = 0.0
        self._target_mic = 0.0
        self._status_text = "IDLE"
        self._interaction_active = False   # True when listening/thinking/speaking
        self._click_through = False
        self._low_perf = False             # performance mode toggle
        self._time = 0.0
        self._bg_intensity = 0.28
        self._current_color = QColor(184, 155, 92)
        self._bg_tint = QColor(6, 12, 22)
        self._bg_cache = None
        self._cached_tint = None
        self._cached_dpr = 1.0
        self._last_time = time.perf_counter()
        self._exiting = False
        self._is_animating = False
        self._activation_bg_flash = 0.0
        self._paint_in_progress = False
        self._update_pending = False
        self._frame_dirty = False
        self._fade_direction = "idle"
        self._frame_budget_ms = _FRAME_INTERVAL_ACTIVE_MS
        self._frame_monitor_started = time.perf_counter()
        self._frame_samples = 0
        self._slow_frame_count = 0
        self._last_paint_cost_ms = 0.0
        self._control_bar_visible = False
        self._chat_panel_visible = False
        self._esc_pressed_last = False
        
        # Connect internal signals for cross-thread HUD control
        self._show_signal.connect(self.show_overlay, Qt.ConnectionType.QueuedConnection)
        self._hide_signal.connect(self.hide_overlay, Qt.ConnectionType.QueuedConnection)
        self._toggle_signal.connect(self.toggle, Qt.ConnectionType.QueuedConnection)
        self._status_signal.connect(self.set_status, Qt.ConnectionType.QueuedConnection)
        self._mic_signal.connect(self.set_mic_level, Qt.ConnectionType.QueuedConnection)
        self._perf_signal.connect(self.set_low_perf, Qt.ConnectionType.QueuedConnection)

        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(6000)
        self._idle_timer.timeout.connect(self.hide_overlay)

        # Expanding waves for speaking
        self._active_waves = [
            {"radius": 0.0, "opacity": 0.0, "initial_opacity": 0.0, "speed": 0.0, "max_dist": 300.0, "active": False}
            for _ in range(4)
        ]
        self._spawn_queue = []
        self._last_spawn_time = 0.0

        # Ambient waves for idle/thinking
        self._ambient_waves = [
            {"radius": 0.0, "opacity": 0.0, "initial_opacity": 0.0, "speed": 0.0, "max_dist": 350.0, "active": False}
            for _ in range(3)
        ]
        self._last_ambient_spawn = 0.0
        self._ambient_current_strength = 1.0

        # Waveform
        self._wave = [0.0] * _WAVE_POINTS
        self._wave_target = [0.0] * _WAVE_POINTS

        # Particles
        self._particles = [
            {
                "angle": random.uniform(0, math.tau),
                "dist": random.uniform(1.3, 2.0),
                "speed": random.uniform(0.25, 0.7),
                "size": random.uniform(1.5, 4.5),
                "alpha": random.uniform(0.25, 0.75),
                "phase": random.uniform(0, math.tau),
            }
            for _ in range(_PARTICLE_COUNT)
        ]

        # Activation flash (brief glow burst when overlay opens)
        self._activation_flash = 0.0

        # ---- animation timer -------------------------------------------
        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.timeout.connect(self._tick)

        # ---- fade animation --------------------------------------------
        self._fade_anim = QPropertyAnimation(self, b"fadeLevel", self)
        self._fade_anim.setDuration(320)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._fade_anim.finished.connect(self._on_fade_animation_finished)

        # ---- interactive control bar (bottom center) --------------------
        self._build_controls()

        # ---- chat panel (below orb) ------------------------------------
        self._chat_panel = OverlayChatPanel(self)
        self._chat_panel.hide()
        self._chat_slide_anim = QPropertyAnimation(self._chat_panel, b"pos", self)
        self._chat_slide_anim.setDuration(240)
        self._chat_slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._chat_slide_anim.finished.connect(self._on_chat_transition_finished)
        self._input_handler = OverlayInputHandler(self)
        self._chat_panel.text_submitted.connect(self._on_chat_submitted)
        self._chat_panel.draft_changed.connect(self._input_handler.on_draft_changed)
        self._input_handler.state_changed.connect(self._on_input_state_changed)
        self._input_handler.submission_requested.connect(self.text_submitted)
        self._chat_panel.history.setMinimumHeight(0)
        self._chat_panel.history.setMaximumHeight(0)
        self._chat_panel.history.hide()
        
        # ---- right-side response panel ----------------------------------
        self._response_panel = QWidget(self)
        self._response_panel.setObjectName("overlayResponsePanel")
        self._response_panel.setFixedWidth(320)
        self._response_panel.setStyleSheet(
            "#overlayResponsePanel {"
            "  background: rgba(10, 10, 10, 0.72);"
            "  border-left: 1px solid rgba(255, 255, 255, 0.08);"
            "  border-radius: 0px;"
            "}"
        )
        screen = QApplication.primaryScreen()
        screen_rect = screen.availableGeometry() if screen else self.geometry()
        self._response_panel.setGeometry(
            max(0, screen_rect.width() - 320),
            0,
            320,
            max(0, screen_rect.height()),
        )
        self._response_panel.hide()

        panel_layout = QVBoxLayout(self._response_panel)
        panel_layout.setContentsMargins(18, 40, 18, 24)
        panel_layout.setSpacing(12)
        panel_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._response_panel_layout = panel_layout

        self._response_label = QLabel(self._response_panel)
        self._response_label.setObjectName("overlayResponseLabel")
        self._response_label.setWordWrap(True)
        self._response_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._response_label.setStyleSheet(
            "color: rgba(255, 255, 255, 0.92);"
            "font-size: 14px;"
            "background: transparent;"
            "border: none;"
            "padding: 0px;"
        )
        self._response_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._response_label.hide()
        self._detach_from_center_stack(self._response_label)
        panel_layout.addWidget(self._response_label)

        self._streaming_widget = StreamingTextLabel(self._response_panel)
        self._streaming_widget.setObjectName("overlayStreamingWidget")
        self._streaming_widget.setWordWrap(True)
        self._streaming_widget.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._streaming_widget.setStyleSheet(
            "color: rgba(180, 220, 200, 0.80);"
            "font-size: 12px;"
            "background: transparent;"
            "border: none;"
        )
        self._streaming_widget.setFrameShape(QFrame.Shape.NoFrame)
        self._streaming_widget.setFrameShadow(QFrame.Shadow.Plain)
        self._streaming_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._streaming_widget.hide()
        self._detach_from_center_stack(self._streaming_widget)
        panel_layout.addWidget(self._streaming_widget)

        self._response_panel_hide_timer = QTimer(self)
        self._response_panel_hide_timer.setSingleShot(True)
        self._response_panel_hide_timer.setInterval(6000)
        self._response_panel_hide_timer.timeout.connect(self._maybe_hide_response_panel)
        self._set_response_panel_visible(False)

        # Ensure chat input stays in the center stack
        self._chat_panel.raise_()

        # ---- global hotkey ---------------------------------------------
        self._hotkey: Optional[_WinHotkey] = None
        self._hotkey_signal.connect(self.toggle)

    # ------------------------------------------------------------------
    # Controls bar
    # ------------------------------------------------------------------
    def _build_controls(self):
        """Minimal floating control strip at the bottom of the overlay."""
        bar = QWidget(self)
        bar.setObjectName("overlayControlBar")
        bar.setFixedSize(280, 48)
        bar.setStyleSheet(
            "#overlayControlBar {"
            "  background: rgba(14,18,24,200);"
            "  border: 1px solid rgba(184,155,92,0.25);"
            "  border-radius: 24px;"
            "}"
            "QPushButton {"
            "  background: rgba(184,155,92,0.18);"
            "  color: #f4ecdf;"
            "  border: 1px solid rgba(184,155,92,0.35);"
            "  border-radius: 16px;"
            "  padding: 6px 18px;"
            "  font-weight: 600;"
            "  font-size: 12px;"
            "}"
            "QPushButton:hover { background: rgba(184,155,92,0.30); }"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        self._btn_interrupt = QPushButton("Interrupt")
        self._btn_interrupt.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._btn_interrupt.clicked.connect(self._on_interrupt_clicked)

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._btn_cancel.clicked.connect(self._on_cancel_clicked)

        self._btn_close = QPushButton("✕")
        self._btn_close.setFixedWidth(36)
        self._btn_close.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._btn_close.clicked.connect(self.hide_overlay)

        layout.addWidget(self._btn_interrupt)
        layout.addWidget(self._btn_cancel)
        layout.addWidget(self._btn_close)

        self._control_bar = bar
        self._control_bar.hide()
        self._control_slide_anim = QPropertyAnimation(self._control_bar, b"pos", self)
        self._control_slide_anim.setDuration(220)
        self._control_slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._control_slide_anim.finished.connect(self._on_control_transition_finished)

    def _position_controls(self):
        """Place the control bar at bottom-center and chat panel below orb."""
        if self.width() <= 0 or self.height() <= 0:
            return

        control_target = (
            self._control_bar_visible_pos()
            if self._control_bar_visible
            else self._control_bar_hidden_pos()
        )
        chat_target = (
            self._chat_panel_visible_pos()
            if self._chat_panel_visible
            else self._chat_panel_hidden_pos()
        )

        if self._control_slide_anim.state() == QPropertyAnimation.State.Running:
            self._control_slide_anim.setEndValue(control_target)
        else:
            self._control_bar.move(control_target)

        if self._chat_slide_anim.state() == QPropertyAnimation.State.Running:
            self._chat_slide_anim.setEndValue(chat_target)
        else:
            self._chat_panel.move(chat_target)
        self._position_response_panel()

    def _position_response_panel(self) -> None:
        if not hasattr(self, "_response_panel") or self.width() <= 0 or self.height() <= 0:
            return
        self._response_panel.setGeometry(max(0, self.width() - 320), 0, 320, self.height())

    def _detach_from_center_stack(self, widget: QWidget) -> None:
        if widget.parentWidget() is self._response_panel:
            return

        root_layout = self.layout()
        if root_layout is not None and root_layout.indexOf(widget) != -1:
            root_layout.removeWidget(widget)

        parent = widget.parentWidget()
        if parent is not None and parent is not self._response_panel:
            parent_layout = parent.layout()
            if parent_layout is not None and parent_layout.indexOf(widget) != -1:
                parent_layout.removeWidget(widget)

        if parent is not self._response_panel:
            widget.setParent(None)

    def _control_bar_visible_pos(self) -> QPoint:
        bw = self._control_bar.width()
        bh = self._control_bar.height()
        return QPoint((self.width() - bw) // 2, self.height() - bh - 60)

    def _control_bar_hidden_pos(self) -> QPoint:
        pos = self._control_bar_visible_pos()
        return QPoint(pos.x(), pos.y() + _CONTROL_HIDE_OFFSET)

    def _chat_panel_visible_pos(self) -> QPoint:
        orb_bottom = int(self.height() / 2) + _ORB_BASE_RADIUS + 65
        pw = self._chat_panel.width()
        return QPoint((self.width() - pw) // 2, orb_bottom)

    def _chat_panel_hidden_pos(self) -> QPoint:
        pos = self._chat_panel_visible_pos()
        return QPoint(pos.x(), pos.y() + _CHAT_HIDE_OFFSET)

    def _animate_control_bar(self, visible: bool) -> None:
        if self._control_bar_visible == visible and self._control_slide_anim.state() != QPropertyAnimation.State.Running:
            return
        self._control_bar_visible = visible
        start = self._control_bar.pos()
        end = self._control_bar_visible_pos() if visible else self._control_bar_hidden_pos()
        self._control_bar.show()
        self._control_bar.setEnabled(visible)
        self._control_slide_anim.stop()
        self._control_slide_anim.setStartValue(start)
        self._control_slide_anim.setEndValue(end)
        self._control_slide_anim.start()

    def _animate_chat_panel(self, visible: bool) -> None:
        if self._chat_panel_visible == visible and self._chat_slide_anim.state() != QPropertyAnimation.State.Running:
            return
        self._chat_panel_visible = visible
        start = self._chat_panel.pos()
        end = self._chat_panel_visible_pos() if visible else self._chat_panel_hidden_pos()
        self._chat_panel.show()
        self._chat_panel.setEnabled(visible)
        self._chat_slide_anim.stop()
        self._chat_slide_anim.setStartValue(start)
        self._chat_slide_anim.setEndValue(end)
        self._chat_slide_anim.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def register_hotkey(self):
        if sys.platform != "win32":
            return
        if self._hotkey is not None:
            return
        self._hotkey = _WinHotkey(self._on_hotkey_pressed)
        self._hotkey.start()

    def unregister_hotkey(self):
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None

    @Slot(float)
    def set_mic_level(self, level: float):
        if QThread.currentThread() != self.thread():
            self._mic_signal.emit(level)
            return
        self._target_mic = max(0.0, min(1.0, level))
        self._sync_frame_loop(immediate=self.isVisible() and self._target_mic > 0.01)

    @Slot(str)
    def set_status(self, text: str):
        if QThread.currentThread() != self.thread():
            self._status_signal.emit(text)
            return
            
        upper = text.upper()
        if upper == "LISTENING" and self._status_text in {"SPEAKING", "THINKING", "RECOGNIZING", "PROCESSING", "EXECUTING"}:
            # Strong visual flash for interrupt/transition
            self._activation_flash = 1.0

        self._status_text = upper
        visual_busy_states = {"LISTENING", "RECOGNIZING", "THINKING", "PROCESSING", "EXECUTING", "RESPONDING", "SPEAKING"}
        input_busy_states = {"RECOGNIZING", "THINKING", "PROCESSING", "EXECUTING", "SPEAKING"}
        streaming_states = {"RECOGNIZING", "THINKING", "PROCESSING", "EXECUTING", "RESPONDING", "SPEAKING"}
        interacting = self._active or upper in visual_busy_states
        if interacting != self._interaction_active:
            self._interaction_active = interacting
            if self._active:
                self._sync_click_through()
                if self._idle_timer.isActive():
                    self._idle_timer.stop()
        if self._active:
            self._sync_control_bar()
        self._chat_panel.set_processing(upper in input_busy_states)
        if hasattr(self, "_streaming_widget"):
            if upper in streaming_states:
                self._response_panel_hide_timer.stop()
                self._streaming_widget.setText(upper)
                self._streaming_widget.show()
                self._set_response_panel_visible(True)
            else:
                self._streaming_widget.clear_stream()
                if self._response_label.text().strip():
                    self._set_response_panel_visible(True)
                elif upper != "TYPING":
                    self._set_response_panel_visible(False)
        if upper in {"IDLE", "TYPING", "LISTENING"} and self._active and not self._chat_panel.input_field.hasFocus():
            QTimer.singleShot(40, self._chat_panel.focus_input)
        self._queue_overlay_update()
        self._sync_frame_loop(immediate=upper in visual_busy_states or self._activation_flash > 0.0)

    def _sync_control_bar(self):
        if QThread.currentThread() != self.thread():
            return # Should only be called internally on main thread
        busy = self._status_text in {"LISTENING", "RECOGNIZING", "THINKING", "PROCESSING", "EXECUTING", "RESPONDING", "SPEAKING"}
        self._animate_control_bar(self._active and busy)

    def set_low_perf(self, enabled: bool):
        """Toggle performance mode: disables particles, halves FPS."""
        if QThread.currentThread() != self.thread():
            self._perf_signal.emit(enabled)
            return
            
        self._low_perf = enabled
        self._frame_budget_ms = self._current_frame_interval_ms()
        self._bg_cache = None
        self._queue_overlay_update()
        self._sync_frame_loop(immediate=self.isVisible())

    def toggle(self):
        """Thread-safe toggle HUD visibility."""
        if QThread.currentThread() != self.thread():
            self._toggle_signal.emit()
            return
            
        if self._is_animating:
            return
            
        if self._active:
            self.hide_overlay()
        else:
            self.show_overlay()

    def show_overlay(self):
        """Fade in HUD. Thread-safe."""
        if QThread.currentThread() != self.thread():
            self._show_signal.emit()
            return

        if self._active or self._is_animating:
            return
            
        self._is_animating = True
        self._active = True
        self._activation_flash = 1.0

        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.geometry()
            self.setGeometry(geo)
        else:
            _overlay_notice("[Overlay] No primary screen detected.")

        # Ensure the window is not invisible
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self._control_bar_visible = False
        self._chat_panel_visible = False
        self._position_controls()

        # CRITICAL: Disable click-through so user can type
        self._click_through = False
        self._set_click_through(False)

        self._fade_anim.stop()
        self._fade_direction = "in"
        self._fade_anim.setDuration(320)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()
        
        self._last_time = time.perf_counter()
        self._exiting = False
        self._activation_bg_flash = 0.4
        
        # Disable auto-hide — user is interacting via text
        if self._idle_timer.isActive():
            self._idle_timer.stop()

        # Show chat panel and focus input
        self._animate_chat_panel(True)
        self._set_response_panel_visible(False)
        self._sync_control_bar()
        self._input_handler.activate()
        QTimer.singleShot(120, self._chat_panel.focus_input)
        self._queue_overlay_update()
        self._sync_frame_loop(immediate=True)

        _play_cue("activate")

    def hide_overlay(self):
        """Fade out HUD. Thread-safe."""
        if QThread.currentThread() != self.thread():
            self._hide_signal.emit()
            return

        if not self._active or self._is_animating:
            return
            
        self._is_animating = True
        self._active = False
        self._exiting = True

        # Hide chat panel
        self._chat_panel.set_processing(False)
        self._input_handler.cancel()
        self._animate_chat_panel(False)
        self._set_response_panel_visible(False)
        
        if self._idle_timer.isActive():
            self._idle_timer.stop()

        self._animate_control_bar(False)

        _play_cue("deactivate")

        if self._fade_anim.state() == QPropertyAnimation.State.Running:
            self._fade_anim.stop()
            
        self._fade_direction = "out"
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setDuration(450)   # slightly longer exit
        self._fade_anim.start()
        self._queue_overlay_update()
        self._sync_frame_loop(immediate=True)

    # ------------------------------------------------------------------
    # Click-through management
    # ------------------------------------------------------------------
    def _sync_click_through(self):
        """Enable click-through when idle, disable when interaction is active."""
        want = not self._interaction_active
        if want == self._click_through:
            return
        self._click_through = want
        self._set_click_through(want)
        if not want:
            # Bring to front so buttons are clickable
            self.raise_()
            self.activateWindow()

    def _set_click_through(self, enable: bool):
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if enable:
                style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
            else:
                style = (style | WS_EX_LAYERED) & ~WS_EX_TRANSPARENT
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception as e:
            _overlay_notice(f"[Overlay] Click-through toggle failed: {e}")

    def _queue_overlay_update(self):
        """Coalesce repaint requests so timer and animations do not overlap paints."""
        self._frame_dirty = True
        if not self.isVisible() or self.width() <= 0 or self.height() <= 0:
            return
        if self._paint_in_progress or self._update_pending:
            return
        self._update_pending = True
        self.update()

    def _current_frame_interval_ms(self) -> int:
        return _FRAME_INTERVAL_IDLE_MS if self._low_perf else _FRAME_INTERVAL_ACTIVE_MS

    def _ensure_frame_timer(self, *, immediate: bool = False) -> None:
        if not self.isVisible():
            return
        interval = 0 if immediate else self._current_frame_interval_ms()
        self._frame_budget_ms = max(1, self._current_frame_interval_ms())
        if self._frame_timer.isActive():
            if immediate:
                self._frame_timer.stop()
                self._frame_timer.start(0)
            return
        self._frame_timer.start(interval)

    def _sync_frame_loop(self, *, immediate: bool = False) -> None:
        if not self.isVisible():
            if self._frame_timer.isActive():
                self._frame_timer.stop()
            return

        active_waves = any(w["active"] for w in self._active_waves)
        ambient_waves = any(w["active"] for w in self._ambient_waves)
        busy_visual_states = {"LISTENING", "RECOGNIZING", "THINKING", "PROCESSING", "EXECUTING", "RESPONDING", "SPEAKING"}
        transitions_pending = (
            self._fade_anim.state() == QPropertyAnimation.State.Running
            or self._control_slide_anim.state() == QPropertyAnimation.State.Running
            or self._chat_slide_anim.state() == QPropertyAnimation.State.Running
        )
        needs_animation = (
            self._status_text in busy_visual_states
            or self._exiting
            or transitions_pending
            or abs(self._target_mic - self._mic_level) > 0.01
            or self._activation_flash > 0.01
            or self._activation_bg_flash > 0.01
            or active_waves
            or ambient_waves
            or bool(self._spawn_queue)
            or self._wave_has_energy()
        )

        if needs_animation:
            self._ensure_frame_timer(immediate=immediate)
        elif self._frame_timer.isActive():
            self._frame_timer.stop()

    def _wave_has_energy(self) -> bool:
        return any(abs(v) > 0.01 for v in self._wave)

    def _color_delta(self, c1: QColor, c2: QColor) -> int:
        return abs(c1.red() - c2.red()) + abs(c1.green() - c2.green()) + abs(c1.blue() - c2.blue())

    def _record_paint_cost(self, cost_ms: float) -> None:
        self._last_paint_cost_ms = cost_ms
        self._frame_samples += 1
        if cost_ms > self._frame_budget_ms * 1.25:
            self._slow_frame_count += 1

        now = time.perf_counter()
        if now - self._frame_monitor_started < 5.0:
            return

        if OVERLAY_VERBOSE:
            _overlay_debug(
                f"[Overlay] paint last={self._last_paint_cost_ms:.2f}ms budget={self._frame_budget_ms}ms "
                f"samples={self._frame_samples} slow={self._slow_frame_count}"
            )
        self._frame_monitor_started = now
        self._frame_samples = 0
        self._slow_frame_count = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _on_hotkey_pressed(self):
        # Safely emit signal to transition to the main GUI thread
        _overlay_debug("[Overlay] Dispatching hotkey event to GUI thread")
        self._hotkey_signal.emit()

    def _on_fade_animation_finished(self):
        direction = self._fade_direction
        self._fade_direction = "idle"
        self._fade_anim.setDuration(320)
        if direction == "out":
            if self._frame_timer.isActive():
                self._frame_timer.stop()
            self.hide()
            self._control_bar.hide()
            self._chat_panel.hide()
            self._update_pending = False
            self._frame_dirty = False
        self._is_animating = False

    def _on_control_transition_finished(self) -> None:
        if not self._control_bar_visible:
            self._control_bar.hide()

    def _on_chat_transition_finished(self) -> None:
        if not self._chat_panel_visible:
            self._chat_panel.hide()

    def _on_input_state_changed(self, state: str):
        upper = state.upper()
        if upper in {
            OverlaySessionState.IDLE.value,
            OverlaySessionState.TYPING.value,
            OverlaySessionState.PROCESSING.value,
            OverlaySessionState.EXECUTING.value,
            OverlaySessionState.RESPONDING.value,
        }:
            self.set_status(upper)

    def _on_interrupt_clicked(self):
        self.interrupt_requested.emit()

    def _on_cancel_clicked(self):
        self.cancel_requested.emit()

    # ------------------------------------------------------------------
    # Chat integration
    # ------------------------------------------------------------------
    def _on_chat_submitted(self, text: str):
        """Handle text submission from the chat input."""
        self._chat_panel.add_user_message(text)
        self._chat_panel.show_thinking()
        self._input_handler.submit(text)
        # Stop idle timer — we're actively processing
        if self._idle_timer.isActive():
            self._idle_timer.stop()
        if self._active:
            self.raise_()
            self.activateWindow()
        self._queue_overlay_update()
        self._sync_frame_loop(immediate=True)

    @Slot(str)
    def show_response(self, text: str):
        """Display an assistant response in the chat history."""
        if QThread.currentThread() != self.thread():
            # Thread-safe: re-dispatch to main thread
            from PySide6.QtCore import QMetaObject, Q_ARG
            QMetaObject.invokeMethod(
                self, "show_response",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, text),
            )
            return
        self._chat_panel.add_assistant_message(text)
        if hasattr(self, "_response_label"):
            self._response_panel_hide_timer.stop()
            self._response_label.setText(text)
            self._response_label.setVisible(bool(text.strip()))
            if hasattr(self, "_streaming_widget"):
                self._streaming_widget.clear_stream()
            self._set_response_panel_visible(True)
        self._chat_panel.ready_for_next_turn()
        self._input_handler.mark_responding()
        QTimer.singleShot(420, self._input_handler.finish_cycle)
        if self._active:
            QTimer.singleShot(480, self._restore_ready_state)
        self._queue_overlay_update()
        self._sync_frame_loop(immediate=True)

    def _set_response_panel_visible(self, visible: bool) -> None:
        if not hasattr(self, "_response_panel") or not hasattr(self, "_response_label"):
            return
        if visible:
            self._response_panel_hide_timer.stop()
            self._position_response_panel()
            self._response_panel.show()
            self._response_panel.raise_()
            self._response_panel.update()
            return
        self._response_panel_hide_timer.stop()
        if hasattr(self, "_streaming_widget"):
            self._streaming_widget.clear_stream()
        self._response_panel.hide()

    def _maybe_hide_response_panel(self) -> None:
        self._response_panel_hide_timer.stop()

    def _schedule_response_panel_hide(self) -> None:
        self._response_panel_hide_timer.stop()

    def _restore_ready_state(self) -> None:
        if not self._active or self._is_animating:
            return
        if self._status_text not in {"IDLE", "RESPONDING"}:
            return
        self.set_status("LISTENING")

    def show_chat_thinking(self):
        """Show the animated thinking indicator in chat."""
        self._chat_panel.show_thinking()
        self._input_handler.mark_processing()
        self._queue_overlay_update()
        self._sync_frame_loop(immediate=True)

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.hide_overlay()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Animation tick
    # ------------------------------------------------------------------
    def _tick(self):
        if not self.isVisible():
            return

        now = time.perf_counter()
        dt = min(0.05, max(0.001, now - self._last_time))
        self._last_time = now
        self._time += dt
        self._frame_budget_ms = self._current_frame_interval_ms()

        busy_visual_states = {"LISTENING", "RECOGNIZING", "THINKING", "PROCESSING", "EXECUTING", "RESPONDING", "SPEAKING"}
        animate_continuously = self._status_text in busy_visual_states or self._exiting
        visual_changed = animate_continuously
        prev_mic_level = self._mic_level
        prev_bg_intensity = self._bg_intensity
        prev_activation_flash = self._activation_flash
        prev_activation_bg_flash = self._activation_bg_flash
        prev_color = QColor(self._current_color)
        prev_tint = QColor(self._bg_tint)

        if self._active:
            # Poll ESC globally to ensure dismissal regardless of focus
            if sys.platform == "win32" and ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000:
                if not getattr(self, "_esc_pressed_last", False):
                    self.hide_overlay()
                self._esc_pressed_last = True
            else:
                self._esc_pressed_last = False

        # Color shift and BG intensity for states
        fade_speed = 4.5
        
        if self._status_text in {"RECOGNIZING", "THINKING", "PROCESSING"}:
            target_c = QColor(160, 100, 220)  # Purple
            target_bg_tint = QColor(16, 10, 28)
            pulse_speed = 1.0
            pulse_amp = 0.0 if self._low_perf else 0.02
            target_bg = 0.45 + math.sin(self._time * 1.2) * pulse_amp
            fade_speed = 1.5
        elif self._status_text == "LISTENING":
            target_c = QColor(184, 155, 92)  # Gold
            target_bg_tint = QColor(6, 12, 22)
            pulse_speed = 2.0
            target_bg = 0.40
            fade_speed = 7.5
        elif self._status_text in {"EXECUTING", "RESPONDING", "SPEAKING"}:
            target_c = QColor(80, 200, 180)  # Cyan
            target_bg_tint = QColor(4, 16, 20)
            pulse_speed = 1.8
            target_bg = 0.35
            fade_speed = 4.5
        elif self._status_text == "TYPING":
            target_c = QColor(212, 178, 106)
            target_bg_tint = QColor(10, 14, 24)
            pulse_speed = 1.7
            target_bg = 0.33
            fade_speed = 5.0
        else:
            target_c = QColor(184, 155, 92)  # Gold
            target_bg_tint = QColor(6, 12, 22)
            pulse_speed = 1.5
            target_bg = 0.28
            fade_speed = 3.0

        if self._exiting:
            target_c = QColor(100, 100, 100)
            target_bg_tint = QColor(10, 10, 10)
            fade_speed = 10.0

        # Limit tint blending strength to max 35% of base neutral
        tr = int(6 * 0.65 + target_bg_tint.red() * 0.35)
        tg = int(12 * 0.65 + target_bg_tint.green() * 0.35)
        tb = int(22 * 0.65 + target_bg_tint.blue() * 0.35)
        target_bg_tint = QColor(tr, tg, tb)

        def gamma_lerp(c1, c2, factor):
            return math.sqrt(max(0.0, c1**2 + (c2**2 - c1**2) * factor))

        color_factor = 1.0 - math.exp(-6.0 * dt)
        r = gamma_lerp(self._current_color.red(), target_c.red(), color_factor)
        g = gamma_lerp(self._current_color.green(), target_c.green(), color_factor)
        b = gamma_lerp(self._current_color.blue(), target_c.blue(), color_factor)
        self._current_color = QColor(int(r), int(g), int(b))

        tint_factor = 1.0 - math.exp(-3.0 * dt)
        tr_l = gamma_lerp(self._bg_tint.red(), target_bg_tint.red(), tint_factor)
        tg_l = gamma_lerp(self._bg_tint.green(), target_bg_tint.green(), tint_factor)
        tb_l = gamma_lerp(self._bg_tint.blue(), target_bg_tint.blue(), tint_factor)
        self._bg_tint = QColor(int(tr_l), int(tg_l), int(tb_l))

        # Smooth mic level and background dimming (dt-based)
        mic_factor = 1.0 - math.exp(-15.0 * dt)
        self._mic_level += (self._target_mic - self._mic_level) * mic_factor
        
        bg_factor = 1.0 - math.exp(-fade_speed * dt)
        self._bg_intensity += (target_bg - self._bg_intensity) * bg_factor

        # Activation flash decay
        if self._activation_flash > 0.01:
            self._activation_flash *= math.exp(-4.0 * dt)
        else:
            self._activation_flash = 0.0

        if self._activation_bg_flash > 0.01:
            self._activation_bg_flash *= math.exp(-8.0 * dt)
        else:
            self._activation_bg_flash = 0.0

        # Waveform target
        mic = self._mic_level
        t = self._time
        wave_changed = False
        for i in range(_WAVE_POINTS):
            if animate_continuously or mic > 0.02:
                phase = (i / _WAVE_POINTS) * math.pi * 4
                base = math.sin(phase + t * pulse_speed) * 0.25
                voice = math.sin(phase * 1.6 + t * 3.2) * mic * 0.8
                noise = random.uniform(-0.04, 0.04) * mic
                self._wave_target[i] = base + voice + noise
            else:
                self._wave_target[i] = 0.0

        # Fast follow on wave (low lag)
        for i in range(_WAVE_POINTS):
            prev_wave = self._wave[i]
            self._wave[i] += (self._wave_target[i] - self._wave[i]) * 0.50
            wave_changed = wave_changed or abs(self._wave[i] - prev_wave) > 0.002

        # Speaking Waves logic
        if self._status_text == "SPEAKING" and self._mic_level > 0.12:
            spawn_interval = max(0.08, 0.12 - self._mic_level * 0.05)
            if self._time - self._last_spawn_time > spawn_interval:
                self._spawn_queue.append((self._time + 0.02, self._mic_level))
                self._last_spawn_time = self._time
                visual_changed = True

        active_spawns = [s for s in self._spawn_queue if self._time >= s[0]]
        self._spawn_queue = [s for s in self._spawn_queue if self._time < s[0]]

        for spawn_time, mic_val in active_spawns:
            del spawn_time
            for w in self._active_waves:
                if not w["active"]:
                    w["active"] = True
                    w["radius"] = _ORB_BASE_RADIUS
                    w["initial_opacity"] = min(1.0, mic_val * 1.5)
                    w["opacity"] = w["initial_opacity"]
                    w["speed"] = 220 + mic_val * 100
                    w["max_dist"] = 200 + mic_val * 150
                    visual_changed = True
                    break

        for w in self._active_waves:
            if w["active"]:
                prev_radius = w["radius"]
                prev_opacity = w["opacity"]
                w["radius"] += w["speed"] * dt
                w["speed"] *= math.exp(-1.5 * dt)  # ease-out expansion
                dist = w["radius"] - _ORB_BASE_RADIUS
                progress = dist / w["max_dist"]
                
                # non-linear opacity fade
                w["opacity"] = w["initial_opacity"] * (1.0 - min(1.0, progress**1.5))
                
                if w["opacity"] <= 0.01 or progress >= 1.0:
                    w["active"] = False
                    visual_changed = True
                elif abs(w["radius"] - prev_radius) > 0.02 or abs(w["opacity"] - prev_opacity) > 0.002:
                    visual_changed = True

        # Ambient Waves logic
        amb_interval = 1.2
        amb_base_speed = 120.0
        amb_base_op = 0.05
        amb_target_strength = 0.0
        
        if self._status_text == "LISTENING":
            amb_interval = 1.0
            amb_base_speed = 150.0
            amb_base_op = 0.08
            amb_target_strength = 1.0
        elif self._status_text in {"RECOGNIZING", "THINKING", "PROCESSING", "EXECUTING", "RESPONDING"}:
            amb_interval = 0.9
            amb_base_speed = 140.0
            amb_base_op = 0.12
            amb_target_strength = 0.8
        elif self._status_text == "SPEAKING":
            amb_target_strength = 0.35
            
        if self._exiting:
            amb_target_strength = 0.0

        self._ambient_current_strength += (amb_target_strength - self._ambient_current_strength) * (1.0 - math.exp(-2.0 * dt))

        if self._ambient_current_strength > 0.01:
            if self._time - self._last_ambient_spawn > amb_interval:
                for w in self._ambient_waves:
                    if not w["active"]:
                        w["active"] = True
                        w["radius"] = _ORB_BASE_RADIUS
                        w["initial_opacity"] = amb_base_op * self._ambient_current_strength
                        w["opacity"] = 0.0
                        w["speed"] = amb_base_speed
                        w["max_dist"] = 350.0
                        self._last_ambient_spawn = self._time
                        visual_changed = True
                        break

        for w in self._ambient_waves:
            if w["active"]:
                prev_radius = w["radius"]
                prev_opacity = w["opacity"]
                w["radius"] += w["speed"] * dt
                w["speed"] *= math.exp(-0.8 * dt) # very slow ease-out
                dist = w["radius"] - _ORB_BASE_RADIUS
                progress = min(1.0, dist / w["max_dist"])
                
                if progress < 0.2:
                    fade = progress / 0.2
                else:
                    fade = 1.0 - ((progress - 0.2) / 0.8)**1.2
                    
                w["opacity"] = w["initial_opacity"] * max(0.0, fade)
                
                if progress >= 1.0:
                    w["active"] = False
                    visual_changed = True
                elif abs(w["radius"] - prev_radius) > 0.02 or abs(w["opacity"] - prev_opacity) > 0.002:
                    visual_changed = True

        # Particles (skip in low-perf)
        if not self._low_perf:
            for p in self._particles:
                p["angle"] += p["speed"] * dt
                p["dist"] = 1.3 + math.sin(t * p["speed"] + p["phase"]) * 0.25 + mic * 0.4
            if animate_continuously or mic > 0.02:
                visual_changed = True

        visual_changed = (
            visual_changed
            or abs(self._mic_level - prev_mic_level) > 0.002
            or abs(self._bg_intensity - prev_bg_intensity) > 0.002
            or abs(self._activation_flash - prev_activation_flash) > 0.002
            or abs(self._activation_bg_flash - prev_activation_bg_flash) > 0.002
            or self._color_delta(self._current_color, prev_color) > 1
            or self._color_delta(self._bg_tint, prev_tint) > 1
            or wave_changed
        )

        if visual_changed:
            self._queue_overlay_update()

        needs_more_frames = (
            animate_continuously
            or abs(self._target_mic - self._mic_level) > 0.01
            or abs(target_bg - self._bg_intensity) > 0.005
            or self._activation_flash > 0.01
            or self._activation_bg_flash > 0.01
            or self._color_delta(self._current_color, target_c) > 3
            or self._color_delta(self._bg_tint, target_bg_tint) > 3
            or bool(self._spawn_queue)
            or any(w["active"] for w in self._active_waves)
            or any(w["active"] for w in self._ambient_waves)
            or abs(self._ambient_current_strength - amb_target_strength) > 0.01
            or self._wave_has_energy()
        )
        if needs_more_frames:
            self._ensure_frame_timer()

    # ------------------------------------------------------------------
    # Resize
    # ------------------------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_controls()
        if hasattr(self, "_response_panel"):
            self._position_response_panel()
        self._bg_cache = None

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        del event
        if self._paint_in_progress:
            return

        self._paint_in_progress = True
        self._update_pending = False
        self._frame_dirty = False
        paint_started = time.perf_counter()
        painter = QPainter(self)
        if not painter.isActive():
            self._paint_in_progress = False
            return

        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            w, h = self.width(), self.height()
            cx, cy = w / 2, h / 2

            self._paint_bg(painter, w, h, cx, cy)
            self._paint_ambient_waves(painter, cx, cy)
            self._paint_wave(painter, w, cy)
            self._paint_expanding_waves(painter, cx, cy)
            if not self._low_perf:
                self._paint_particles(painter, cx, cy)
            self._paint_orb(painter, cx, cy)
            # self._paint_status(painter, cx, cy)  # Replaced by _response_label widget
            self._paint_hint(painter, w, h)
        finally:
            if painter.isActive():
                painter.end()
            self._record_paint_cost((time.perf_counter() - paint_started) * 1000.0)
            self._paint_in_progress = False
            if self._frame_dirty and self.isVisible():
                QTimer.singleShot(0, self._queue_overlay_update)

    # --- background (adaptive dimming) ----------------------------------
    def _paint_bg(self, p: QPainter, w, h, cx, cy):
        br, bg, bb = self._bg_tint.red(), self._bg_tint.green(), self._bg_tint.blue()
        intensity = max(0.0, min(1.0, self._bg_intensity + self._activation_bg_flash))
        dpr = p.device().devicePixelRatio()

        # Low performance mode: cache the gradient rendering
        if self._low_perf:
            # We quantize tint slightly so we don't invalidate cache constantly
            tint_key = (br // 2, bg // 2, bb // 2)
            if self._bg_cache is None or self._cached_tint != tint_key or self._cached_dpr != dpr:
                cache_width = max(1, int(round(w * dpr)))
                cache_height = max(1, int(round(h * dpr)))
                self._bg_cache = QPixmap(cache_width, cache_height)
                self._bg_cache.setDevicePixelRatio(dpr)
                self._bg_cache.fill(Qt.GlobalColor.transparent)
                cache_painter = QPainter()
                if cache_painter.begin(self._bg_cache):
                    try:
                        self._render_bg_gradients(cache_painter, w, h, cx, cy, br, bg, bb, 1.0)
                    finally:
                        cache_painter.end()
                else:
                    self._bg_cache = None
                    return
                self._cached_tint = tint_key
                self._cached_dpr = dpr
            
            p.setOpacity(intensity)
            p.drawPixmap(0, 0, self._bg_cache)
            p.setOpacity(1.0)
        else:
            self._render_bg_gradients(p, w, h, cx, cy, br, bg, bb, intensity)

    def _render_bg_gradients(self, p: QPainter, w, h, cx, cy, br, bg, bb, intensity):
        # Clamped alpha levels
        alpha_center = max(0, min(255, int(255 * (intensity - 0.12))))
        alpha_mid = max(0, min(255, int(255 * (intensity - 0.04))))
        alpha_edge = max(0, min(255, int(255 * intensity)))

        # Reduced radius for stronger center focus (~75% of screen height/width)
        radius = min(w, h) * 0.75
        grad = QRadialGradient(cx, cy, radius)
        grad.setColorAt(0.0, QColor(br, bg, bb, alpha_center))
        grad.setColorAt(0.45, QColor(br, bg, bb, alpha_mid))
        grad.setColorAt(1.0, QColor(br, bg, bb, alpha_edge))
        p.fillRect(0, 0, w, h, grad)

        # Eased vignette edges (only in high-perf)
        if not self._low_perf and alpha_edge > 10:
            v_alpha = min(255, alpha_edge + 40)
            
            def make_eased_gradient(y1, y2):
                grad = QLinearGradient(0, y1, 0, y2)
                grad.setColorAt(0.0, QColor(br // 2, bg // 2, bb // 2, v_alpha))
                grad.setColorAt(0.4, QColor(br // 2, bg // 2, bb // 2, int(v_alpha * 0.7)))
                grad.setColorAt(0.7, QColor(br // 2, bg // 2, bb // 2, int(v_alpha * 0.2)))
                grad.setColorAt(1.0, QColor(br // 2, bg // 2, bb // 2, 0))
                return grad
            
            # Top
            p.fillRect(0, 0, w, int(h * 0.15), make_eased_gradient(0, h * 0.15))
            
            # Bottom
            p.fillRect(0, int(h * 0.85), w, int(h * 0.15), make_eased_gradient(h, h * 0.85))

    # --- waveform -------------------------------------------------------
    def _paint_wave(self, p: QPainter, w, cy):
        spacing = w / (_WAVE_POINTS - 1)
        amp = 100 + self._mic_level * 80

        c_r, c_g, c_b = self._current_color.red(), self._current_color.green(), self._current_color.blue()

        layers = [
            (QColor(c_r, c_g, c_b, 40), 3.5, 0.70),
            (QColor(c_r, c_g, c_b, 80), 2.0, 1.00),
            (QColor(244, 236, 223, 110), 1.2, 1.15),
        ]

        for color, width, scale in layers:
            pen = QPen(color, width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)

            path = QPainterPath()
            for i, v in enumerate(self._wave):
                x = i * spacing
                y = cy + v * amp * scale
                if i == 0:
                    path.moveTo(x, y)
                else:
                    px = (i - 1) * spacing
                    py = cy + self._wave[i - 1] * amp * scale
                    mx = (px + x) / 2
                    path.cubicTo(mx, py, mx, y, x, y)
            p.drawPath(path)

    # --- expanding waves ------------------------------------------------
    def _paint_expanding_waves(self, p: QPainter, cx, cy):
        c_r, c_g, c_b = self._current_color.red(), self._current_color.green(), self._current_color.blue()
        p.setBrush(Qt.BrushStyle.NoBrush)

        for w in self._active_waves:
            if w["active"] and w["opacity"] > 0.01:
                thickness = max(0.1, 2.0 * w["opacity"])
                alpha = int(255 * w["opacity"])
                pen = QPen(QColor(c_r, c_g, c_b, alpha), thickness)
                p.setPen(pen)
                r = w["radius"]
                p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

    # --- ambient waves --------------------------------------------------
    def _paint_ambient_waves(self, p: QPainter, cx, cy):
        # Soft cyan-blue
        c_r, c_g, c_b = 100, 180, 220
        p.setBrush(Qt.BrushStyle.NoBrush)

        for w in self._ambient_waves:
            if w["active"] and w["opacity"] > 0.005:
                thickness = 1.0 + (w["opacity"] * 2.0)
                alpha = int(255 * w["opacity"])
                pen = QPen(QColor(c_r, c_g, c_b, alpha), thickness)
                p.setPen(pen)
                r = w["radius"]
                p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

    # --- particles ------------------------------------------------------
    def _paint_particles(self, p: QPainter, cx, cy):
        r = _ORB_BASE_RADIUS
        p.setPen(Qt.PenStyle.NoPen)
        c_r, c_g, c_b = self._current_color.red(), self._current_color.green(), self._current_color.blue()
        
        for pt in self._particles:
            x = cx + math.cos(pt["angle"]) * r * pt["dist"]
            y = cy + math.sin(pt["angle"]) * r * pt["dist"]
            a = int(pt["alpha"] * 255 * (0.45 + self._mic_level * 0.55))
            sz = pt["size"] * (1.0 + self._mic_level * 0.5)
            p.setBrush(QColor(c_r, c_g, c_b, a))
            p.drawEllipse(QRectF(x - sz / 2, y - sz / 2, sz, sz))

    # --- orb ------------------------------------------------------------
    def _paint_orb(self, p: QPainter, cx, cy):
        # subtle idle pulse via _time
        pulse_speed = 1.0 if self._status_text == "THINKING" else 2.0
        pulse = 1.0 + math.sin(self._time * pulse_speed) * 0.025 + self._mic_level * 0.12
        r = _ORB_BASE_RADIUS * pulse

        # Activation flash boost (extra glow on open)
        flash_extra = self._activation_flash * 30
        c_r, c_g, c_b = self._current_color.red(), self._current_color.green(), self._current_color.blue()

        # Multi-layer glow
        glow_layers = 4 if self._low_perf else 6
        for i in range(glow_layers):
            gr = r + (glow_layers - i) * 9 + self._mic_level * 18 + flash_extra
            a = max(1, int((12 + self._mic_level * 12 + self._activation_flash * 20) * (i + 1) / glow_layers))
            glow = QRadialGradient(cx, cy, gr)
            glow.setColorAt(0.0, QColor(c_r, c_g, c_b, a))
            glow.setColorAt(0.65, QColor(c_r, c_g, c_b, a // 3))
            glow.setColorAt(1.0, QColor(c_r, c_g, c_b, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(QRectF(cx - gr, cy - gr, gr * 2, gr * 2))

        # Outer ring
        p.setPen(QPen(QColor(c_r, c_g, c_b, 160), 2))
        p.setBrush(QColor(14, 18, 24, 215))
        p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # Inner ring
        ir = r * 0.72
        p.setPen(QPen(QColor(c_r, c_g, c_b, 80), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(cx - ir, cy - ir, ir * 2, ir * 2))

        # Rotating arcs
        ar = r * 0.84
        arc_rect = QRectF(cx - ar, cy - ar, ar * 2, ar * 2)
        arc_pen = QPen(QColor(244, 236, 223, 100), 1.5)
        p.setPen(arc_pen)
        deg = int(self._time * 100) % 360
        p.drawArc(arc_rect, deg * 16, 80 * 16)
        p.drawArc(arc_rect, (deg + 180) * 16, 80 * 16)

        # "J" letter
        p.setFont(QFont("Bahnschrift SemiBold", int(r * 0.48)))
        p.setPen(QColor(244, 236, 223, 210))
        p.drawText(QRectF(cx - r, cy - r, r * 2, r * 2), Qt.AlignmentFlag.AlignCenter, "J")

    # --- status text ----------------------------------------------------
    def _paint_status(self, p: QPainter, cx, cy):
        y = cy + _ORB_BASE_RADIUS + 38
        p.setFont(QFont("Segoe UI Semibold", 11))
        p.setPen(QColor(self._current_color.red(), self._current_color.green(), self._current_color.blue(), 170))
        p.drawText(QRectF(cx - 220, y, 440, 28), Qt.AlignmentFlag.AlignCenter, self._status_text)

    # --- bottom hint ----------------------------------------------------
    def _paint_hint(self, p: QPainter, w, h):
        p.setFont(QFont("Segoe UI", 10))
        p.setPen(QColor(130, 140, 150, 120))
        p.drawText(
            QRectF(0, h - 50, w, 24),
            Qt.AlignmentFlag.AlignCenter,
            "Enter to send · ESC to dismiss",
        )
