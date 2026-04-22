from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from assistant.event_bus import EventBus


@dataclass(slots=True)
class ScheduledEvent:
    run_at: datetime
    topic: str
    payload: dict[str, object]


class Scheduler:
    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._pending: list[ScheduledEvent] = []

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def schedule(self, run_at: datetime, topic: str, payload: dict[str, object]) -> None:
        self._pending.append(ScheduledEvent(run_at=run_at, topic=topic, payload=payload))
        self._pending.sort(key=lambda event: event.run_at)

    def drain_due(self) -> list[dict[str, object]]:
        now_aware = datetime.now(timezone.utc)
        now_naive = datetime.now()
        due: list[ScheduledEvent] = []
        future: list[ScheduledEvent] = []
        for event in self._pending:
            current_now = now_aware if event.run_at.tzinfo is not None else now_naive
            if event.run_at <= current_now:
                due.append(event)
            else:
                future.append(event)
        self._pending = future
        for event in due:
            self._event_bus.publish(event.topic, event.payload)
        return [event.payload for event in due]
