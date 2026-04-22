from __future__ import annotations

from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout

from ui.state import AppState


class MetricTile(QFrame):
    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("metricTile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(4)

        title = QLabel(label)
        title.setObjectName("metricLabel")
        self.value = QLabel("--")
        self.value.setObjectName("metricValue")

        layout.addWidget(title)
        layout.addWidget(self.value)


class MetricsPanel(QFrame):
    def __init__(self, state: AppState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        eyebrow = QLabel("TELEMETRY")
        eyebrow.setObjectName("sectionEyebrow")
        title = QLabel("System and model health")
        title.setObjectName("panelTitle")

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        self.cpu_tile = MetricTile("CPU")
        self.ram_tile = MetricTile("RAM")
        self.gpu_tile = MetricTile("GPU")

        grid.addWidget(self.cpu_tile, 0, 0)
        grid.addWidget(self.ram_tile, 0, 1)
        grid.addWidget(self.gpu_tile, 1, 0, 1, 2)

        self.backend_status = QLabel("Backend: Offline")
        self.backend_status.setObjectName("runtimeStatus")
        self.asr_status = QLabel("Whisper: Loading")
        self.asr_status.setObjectName("runtimeStatus")
        self.tts_status = QLabel("TTS: Loading")
        self.tts_status.setObjectName("runtimeStatus")

        root.addWidget(eyebrow)
        root.addWidget(title)
        root.addLayout(grid)
        root.addWidget(self.backend_status)
        root.addWidget(self.asr_status)
        root.addWidget(self.tts_status)

        state.metrics_changed.connect(self._on_metrics_changed)
        state.backend_online_changed.connect(self._on_backend_online_changed)
        state.model_status_changed.connect(self._on_model_status_changed)

        self._on_metrics_changed(state.metrics)
        self._on_backend_online_changed(state.backend_online)
        self._on_model_status_changed(state.model_status)

    def _on_metrics_changed(self, metrics: dict) -> None:
        self.cpu_tile.value.setText(f"{metrics.get('cpu', 0.0):.0f}%")
        self.ram_tile.value.setText(f"{metrics.get('ram', 0.0):.0f}%")
        gpu = metrics.get("gpu")
        self.gpu_tile.value.setText("N/A" if gpu is None else f"{float(gpu):.0f}%")

    def _on_backend_online_changed(self, online: bool) -> None:
        self.backend_status.setText(f"Backend: {'Online' if online else 'Offline'}")

    def _on_model_status_changed(self, status: dict[str, str]) -> None:
        self.asr_status.setText(f"Whisper: {status.get('asr', 'Unknown')}")
        self.tts_status.setText(f"TTS: {status.get('tts', 'Unknown')}")
