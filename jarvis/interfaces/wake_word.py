from __future__ import annotations

from dataclasses import dataclass
import re


_WAKE_PATTERN = re.compile(r"\b(?:hey jarvis|hi jarvis|jarvis)\b", re.IGNORECASE)
_LEADING_NOISE_PATTERN = re.compile(r"^[^a-z0-9]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class VoiceTranscriptDecision:
    accepted: bool
    command: str = ""
    activated: bool = False
    keep_listening: bool = False


def contains_wake_phrase(text: str) -> bool:
    return bool(_WAKE_PATTERN.search(str(text or "")))


def strip_wake_phrase(text: str) -> str:
    cleaned = _WAKE_PATTERN.sub("", str(text or "")).strip()
    return _LEADING_NOISE_PATTERN.sub("", cleaned).strip()


def extract_command_after_wake(text: str) -> str:
    return strip_wake_phrase(text)


def route_voice_transcript(text: str, *, in_wake_window: bool) -> VoiceTranscriptDecision:
    cleaned = str(text or "").strip()
    if not cleaned:
        return VoiceTranscriptDecision(accepted=False)
    if in_wake_window:
        return VoiceTranscriptDecision(accepted=True, command=cleaned)
    if not contains_wake_phrase(cleaned):
        return VoiceTranscriptDecision(accepted=False)
    command = extract_command_after_wake(cleaned)
    return VoiceTranscriptDecision(
        accepted=bool(command),
        command=command,
        activated=True,
        keep_listening=not bool(command),
    )
