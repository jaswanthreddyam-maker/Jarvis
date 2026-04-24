from __future__ import annotations

from PySide6.QtCore import Signal

from ui.workers.listener_worker import ListenerWorker


class WakeListener(ListenerWorker):
    wake_detected = Signal()

    def start_listening_continuous(self) -> None:
        self.start_listening()

    def stop_listening_continuous(self) -> None:
        self.stop_listening()

