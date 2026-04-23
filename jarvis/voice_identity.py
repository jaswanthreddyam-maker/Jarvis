from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VoiceMode(str, Enum):
    PENDING = "pending"
    AUTHENTICATED = "authenticated"
    GUEST = "guest"


@dataclass
class _VoiceIdentity:
    is_enrolled: bool = False
    current_mode: VoiceMode = VoiceMode.PENDING
    _samples: int = 0

    def clear_enrollment(self) -> None:
        self.is_enrolled = False
        self.current_mode = VoiceMode.PENDING
        self._samples = 0

    def add_enrollment_sample(self, audio, *, sample_rate: int = 16000) -> bool:
        del audio, sample_rate
        self._samples += 1
        if self._samples >= 3:
            self.is_enrolled = True
            self.current_mode = VoiceMode.AUTHENTICATED
            return True
        return False


voice_id = _VoiceIdentity()
voice_id.Mode = VoiceMode
