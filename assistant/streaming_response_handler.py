from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from assistant.event_bus import EventBus


@dataclass(slots=True)
class StreamUpdate:
    task_id: str
    text: str
    lines: tuple[str, ...]
    final: bool = False


class StreamingResponseHandler:
    """Formats execution bus events into UI-friendly progressive text."""

    def __init__(self, event_bus: EventBus) -> None:
        self._subscribers: list[Callable[[StreamUpdate], None]] = []
        self._task_lines: dict[str, list[str]] = {}
        self._finalized_tasks: set[str] = set()
        event_bus.subscribe("execution.step_start", self._on_step_start)
        event_bus.subscribe("execution.step_done", self._on_step_done)
        event_bus.subscribe("execution.step_failed", self._on_step_failed)
        event_bus.subscribe("execution.step_skipped", self._on_step_skipped)
        event_bus.subscribe("execution.cancel_requested", self._on_cancel_requested)
        event_bus.subscribe("execution.completed", self._on_completed)
        event_bus.subscribe("execution.interrupted", self._on_interrupted)

    def subscribe(self, callback: Callable[[StreamUpdate], None]) -> None:
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[StreamUpdate], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _on_step_start(self, payload: dict[str, object]) -> None:
        task_id = str(payload.get("task_id", "")).strip()
        description = str(payload.get("description", "")).strip()
        if not task_id:
            return
        line = f"-> {self._progressive(description)}"
        self._append(task_id, line, final=False)

    def _on_step_done(self, payload: dict[str, object]) -> None:
        task_id = str(payload.get("task_id", "")).strip()
        message = str(payload.get("message", "")).strip()
        if not task_id:
            return
        line = f"-> {self._clean_message(message)}"
        self._append(task_id, line, final=False)

    def _on_step_failed(self, payload: dict[str, object]) -> None:
        task_id = str(payload.get("task_id", "")).strip()
        message = str(payload.get("message", "")).strip()
        if not task_id:
            return
        line = f"-> {self._clean_message(message)}"
        self._append(task_id, line, final=True)

    def _on_step_skipped(self, payload: dict[str, object]) -> None:
        task_id = str(payload.get("task_id", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        if not task_id or not reason:
            return
        self._append(task_id, f"-> {self._clean_message(reason)}", final=False)

    def _on_cancel_requested(self, payload: dict[str, object]) -> None:
        task_id = str(payload.get("task_id", "")).strip()
        if not task_id:
            return
        self._append(task_id, "-> Stopping...", final=False)

    def _on_interrupted(self, payload: dict[str, object]) -> None:
        task_id = str(payload.get("task_id", "")).strip()
        if not task_id:
            return
        self._append(task_id, "-> Stopped.", final=True)

    def _on_completed(self, payload: dict[str, object]) -> None:
        task_id = str(payload.get("task_id", "")).strip()
        if not task_id:
            return
        self._append(task_id, "-> Done.", final=True)

    def _append(self, task_id: str, line: str, *, final: bool) -> None:
        if task_id in self._finalized_tasks:
            return
        lines = self._task_lines.setdefault(task_id, [])
        if not lines or lines[-1] != line:
            lines.append(line)
        update = StreamUpdate(
            task_id=task_id,
            text="\n".join(lines),
            lines=tuple(lines),
            final=final,
        )
        for callback in list(self._subscribers):
            callback(update)
        if final:
            self._task_lines.pop(task_id, None)
            self._finalized_tasks.add(task_id)

    @staticmethod
    def _progressive(description: str) -> str:
        cleaned = description.strip().rstrip(".")
        replacements = (
            ("Open ", "Opening "),
            ("Search ", "Searching "),
            ("Create ", "Creating "),
            ("Delete ", "Deleting "),
            ("Close ", "Closing "),
            ("Focus ", "Focusing "),
            ("Read ", "Reading "),
            ("Schedule ", "Scheduling "),
            ("Store ", "Saving "),
            ("Recall ", "Checking "),
            ("Get ", "Getting "),
            ("Check ", "Checking "),
            ("List ", "Listing "),
        )
        for prefix, replacement in replacements:
            if cleaned.startswith(prefix):
                return replacement + cleaned[len(prefix):] + "..."
        return cleaned + "..."

    @staticmethod
    def _clean_message(message: str) -> str:
        cleaned = " ".join(message.split())
        cleaned = cleaned.replace("[EXECUTED] ", "").replace("[SIMULATED] ", "").replace("[BLOCKED] ", "")
        return cleaned
