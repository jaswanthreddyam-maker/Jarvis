from __future__ import annotations


class VoiceOutput:
    def __init__(self) -> None:
        from jarvis.voice.tts import TTS

        self._tts = TTS()

    @property
    def backend(self):
        return self._tts

    def set_on_finished(self, callback) -> None:
        self._tts.set_on_finished(callback)

    def speak(self, text: str, *, blocking: bool = False) -> None:
        self._tts.speak(text, blocking=blocking)

    def wait(self) -> None:
        self._tts.wait()

    def stop(self) -> None:
        self._tts.stop()
