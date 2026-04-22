from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QCheckBox

from ui.state import AppState


class SettingsPanel(QFrame):
    input_device_selected = Signal(object)
    output_device_selected = Signal(object)
    refresh_requested = Signal()
    test_sound_requested = Signal()
    safe_mode_toggled = Signal(bool)

    def __init__(self, state: AppState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self.setObjectName("panel")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        eyebrow = QLabel("SETTINGS")
        eyebrow.setObjectName("sectionEyebrow")
        title = QLabel("Audio routing")
        title.setObjectName("panelTitle")

        self.input_combo = QComboBox()
        self.input_combo.setObjectName("deviceCombo")
        self.input_combo.currentIndexChanged.connect(self._emit_input_selection)

        self.output_combo = QComboBox()
        self.output_combo.setObjectName("deviceCombo")
        self.output_combo.currentIndexChanged.connect(self._emit_output_selection)

        refresh_button = QPushButton("Refresh Devices")
        refresh_button.setObjectName("secondaryButton")
        refresh_button.clicked.connect(self.refresh_requested.emit)

        test_sound_button = QPushButton("Test Sound")
        test_sound_button.setObjectName("secondaryButton")
        test_sound_button.clicked.connect(self.test_sound_requested.emit)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)
        btn_layout.addWidget(refresh_button)
        btn_layout.addWidget(test_sound_button)

        # --- Safe Mode Toggle ---
        safe_mode_section = QLabel("EXECUTION")
        safe_mode_section.setObjectName("sectionEyebrow")

        self.safe_mode_checkbox = QCheckBox("Safe Mode (block restricted actions)")
        self.safe_mode_checkbox.setObjectName("safeModeToggle")
        self.safe_mode_checkbox.setChecked(False)
        self.safe_mode_checkbox.toggled.connect(self._on_safe_mode_toggled)

        safe_mode_hint = QLabel(
            "When Safe Mode is ON, risky actions such as installs, deletes, and power actions are blocked. "
            "Safe actions still execute normally. Use simulation mode separately when you want dry runs."
        )
        safe_mode_hint.setObjectName("mutedText")
        safe_mode_hint.setWordWrap(True)

        hint = QLabel("During speech playback the listener stays guarded against self-triggering and can still accept an interrupt.")
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)

        root.addWidget(eyebrow)
        root.addWidget(title)
        root.addWidget(QLabel("Input Device"))
        root.addWidget(self.input_combo)
        root.addWidget(QLabel("Output Device"))
        root.addWidget(self.output_combo)
        root.addLayout(btn_layout)
        root.addWidget(hint)
        root.addSpacing(16)
        root.addWidget(safe_mode_section)
        root.addWidget(self.safe_mode_checkbox)
        root.addWidget(safe_mode_hint)

        state.devices_changed.connect(self._sync_devices)
        self._sync_devices(state.devices)

    def set_safe_mode(self, enabled: bool) -> None:
        """Set the safe mode checkbox state from external source."""
        self.safe_mode_checkbox.blockSignals(True)
        self.safe_mode_checkbox.setChecked(enabled)
        self.safe_mode_checkbox.blockSignals(False)

    def _on_safe_mode_toggled(self, checked: bool) -> None:
        self.safe_mode_toggled.emit(checked)

    def _sync_devices(self, devices: dict) -> None:
        self._populate_combo(
            self.input_combo,
            devices.get("inputs", []),
            devices.get("selected_input"),
            placeholder="System Default Input",
        )
        self._populate_combo(
            self.output_combo,
            devices.get("outputs", []),
            devices.get("selected_output"),
            placeholder="System Default Output",
        )

    def _populate_combo(self, combo: QComboBox, items: list[dict], selected: object, placeholder: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(placeholder, None)
        selected_index = 0
        for offset, item in enumerate(items, start=1):
            combo.addItem(item.get("label", f"Device {item.get('id')}"), item.get("id"))
            if item.get("id") == selected:
                selected_index = offset
        combo.setCurrentIndex(selected_index)
        combo.blockSignals(False)

    def _emit_input_selection(self) -> None:
        self.input_device_selected.emit(self.input_combo.currentData())

    def _emit_output_selection(self) -> None:
        self.output_device_selected.emit(self.output_combo.currentData())
