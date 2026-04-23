from __future__ import annotations

from dataclasses import dataclass

from jarvis.runtime.input_handler import RuntimeInput


@dataclass(slots=True)
class RuntimeDecision:
    kind: str
    text: str = ""
    source: str = "text"
    interrupted: bool = False


class DecisionEngine:
    def decide(self, command: RuntimeInput) -> RuntimeDecision:
        cleaned = command.text.strip()
        if not cleaned:
            return RuntimeDecision(kind="ignore")
        return RuntimeDecision(
            kind="execute",
            text=cleaned,
            source=command.source,
            interrupted=command.interrupted,
        )
