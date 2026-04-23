from __future__ import annotations


class ASR:
    def __init__(self, *, tts=None) -> None:
        del tts
        self.interrupted = False

    def start_continuous(self) -> None:
        return

    def listen_continuous(self) -> str:
        return ""

    def clear_interrupt(self) -> None:
        self.interrupted = False

    def stop_continuous(self) -> None:
        return

