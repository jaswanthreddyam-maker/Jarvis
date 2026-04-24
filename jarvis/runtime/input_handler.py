from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvis.interfaces.voice_input import VoiceInput


@dataclass(slots=True)
class RuntimeInput:
    text: str
    source: str
    interrupted: bool = False
    context: Any | None = None


class TextInputHandler:
    async def listen(self, loop) -> RuntimeInput | None:
        try:
            text = await loop.run_in_executor(None, input, "\n[Jarvis Input]> ")
        except EOFError:
            return None
        return RuntimeInput(text=text, source="text")


class VoiceInputHandler:
    def __init__(self, *, tts_backend=None) -> None:
        self._voice_input = VoiceInput(tts=tts_backend)

    def start(self) -> None:
        self._voice_input.start()

    async def listen(self, loop) -> RuntimeInput | None:
        text = await loop.run_in_executor(None, self._voice_input.listen)
        interrupted = self._voice_input.interrupted
        if interrupted:
            self._voice_input.clear_interrupt()
        return RuntimeInput(text=text, source="voice", interrupted=interrupted)

    def stop(self) -> None:
        self._voice_input.stop()
