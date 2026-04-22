from __future__ import annotations

from dataclasses import dataclass, field

from assistant.event_bus import EventBus
from assistant.memory.memory import MemoryManager
from assistant.scheduler import Scheduler
from assistant.settings import Settings


@dataclass(slots=True)
class ActionContext:
    memory: MemoryManager
    scheduler: Scheduler
    settings: Settings
    event_bus: EventBus
    observer: object | None = None
    cancellation_token: object | None = None
    shared_context: dict[str, object] = field(default_factory=dict)
