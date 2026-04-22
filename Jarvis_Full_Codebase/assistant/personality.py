from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Personality:
    name: str = "Jarvis"
    role: str = "Assistant"
    behavior: str = ""
    tone: str = "calm"
    style: str = "short"
    sarcasm: str = "none"

    @classmethod
    def from_file(cls, path: Path) -> "Personality":
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        filtered = {key: value for key, value in data.items() if key in allowed}
        return cls(**filtered)

    def format_response(self, message: str) -> str:
        return message.strip()
