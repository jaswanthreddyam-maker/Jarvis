from __future__ import annotations

import logging
from PySide6.QtCore import QObject, QTimer, Signal, Slot

logger = logging.getLogger("Jarvis.Metrics")


class MetricsWorker(QObject):
    metrics_ready = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._timer: QTimer | None = None

    @Slot()
    def start(self) -> None:
        try:
            if self._timer is not None:
                return
            self._timer = QTimer(self)
            self._timer.setInterval(2000)
            self._timer.timeout.connect(self.collect)
            self._timer.start()
            self.collect()
        except Exception as e:
            logger.error("Failed to start MetricsWorker: %s", e)

    @Slot()
    def stop(self) -> None:
        try:
            if self._timer is None:
                return
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        except Exception as e:
            logger.error("Failed to stop MetricsWorker: %s", e)

    @Slot()
    def collect(self) -> None:
        try:
            cpu = 0.0
            ram = 0.0
            gpu = None
            try:
                import psutil

                cpu = float(psutil.cpu_percent(interval=None))
                ram = float(psutil.virtual_memory().percent)
            except Exception as e:
                logger.error("Failed to collect system metrics: %s", e)
            self.metrics_ready.emit({"cpu": cpu, "ram": ram, "gpu": gpu})
        except Exception as e:
            logger.error("Metrics collection critical error: %s", e)
