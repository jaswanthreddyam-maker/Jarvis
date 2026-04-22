from __future__ import annotations

from typing import Protocol

from assistant.actions.registry import ActionRegistry


class Plugin(Protocol):
    name: str

    def register(self, registry: ActionRegistry) -> None:
        ...
