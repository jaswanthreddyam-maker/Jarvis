from __future__ import annotations


class VoiceInput:
    def __init__(self, *, tts=None) -> None:
        from jarvis.voice.asr import ASR

        self._asr = ASR(tts=tts)

    @property
    def interrupted(self) -> bool:
        return bool(getattr(self._asr, "interrupted", False))

    def start(self) -> None:
        self._asr.start_continuous()

    def listen(self) -> str:
        return self._asr.listen_continuous()

    def clear_interrupt(self) -> None:
        if hasattr(self._asr, "clear_interrupt"):
            self._asr.clear_interrupt()

    def stop(self) -> None:
        self._asr.stop_continuous()
