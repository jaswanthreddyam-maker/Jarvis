from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _GlobalState:
    status: str = "IDLE"

    def set_status(self, status: str) -> None:
        self.status = str(status)


global_state = _GlobalState()

