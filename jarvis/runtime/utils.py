from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def configure_espeak() -> None:
    if sys.platform != "win32":
        return
    if os.environ.get("PHONEMIZER_ESPEAK_LIBRARY"):
        return

    candidates = [
        Path(r"C:\Program Files\eSpeak NG\libespeak-ng.dll"),
        Path(r"C:\Program Files (x86)\eSpeak NG\libespeak-ng.dll"),
    ]
    for candidate in candidates:
        if candidate.exists():
            os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = str(candidate)
            os.environ["PATH"] = str(candidate.parent) + os.pathsep + os.environ.get("PATH", "")
            break


def default_memory_db_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "longterm.db"


def split_sentences(text: str) -> list[str]:
    return [chunk.strip() for chunk in re.split(r"(?<=[.!?])\s+", text.strip()) if chunk.strip()]
