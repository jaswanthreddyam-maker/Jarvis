from __future__ import annotations

import asyncio

from jarvis.interfaces.voice_output import VoiceOutput
from jarvis.runtime.decision_engine import RuntimeDecision
from jarvis.runtime.utils import split_sentences


class OutputHandler:
    def __init__(self, voice_output: VoiceOutput | None = None) -> None:
        self._voice_output = voice_output or VoiceOutput()

    @property
    def backend(self):
        return self._voice_output.backend

    def set_interrupt_callback(self, callback) -> None:
        self._voice_output.set_on_finished(callback)

    def print_banner(self) -> None:
        print("=" * 50)
        print("      JARVIS LOCAL AI ASSISTANT")
        print("      Clean Architecture Runtime")
        print("=" * 50)

    def announce_ready(self, *, voice_mode: bool) -> None:
        greeting = "Jarvis is online and ready."
        print(f"[Jarvis]: {greeting}")
        self._voice_output.speak(greeting, blocking=not voice_mode)
        if voice_mode:
            self._voice_output.wait()
            print("\nReal-time voice mode active. Speak anytime. Press Ctrl+C to quit.\n")
            return
        print("\nType your commands. Press Ctrl+C or send EOF to quit.\n")

    async def announce_ready_async(self, *, voice_mode: bool) -> None:
        await asyncio.to_thread(self.announce_ready, voice_mode=voice_mode)

    def show_input(self, decision: RuntimeDecision) -> None:
        if decision.source != "voice":
            return
        label = "[Interrupted -> New command]" if decision.interrupted else "[You said]"
        print(f"{label}: {decision.text}")

    def respond(self, response: str) -> None:
        print(f"[Jarvis]: {response}")
        self._speak_streamed(response)

    async def respond_async(self, response: str) -> None:
        await asyncio.to_thread(self.respond, response)

    def say_farewell(self, text: str = "Shutting down. Goodbye.") -> None:
        print(f"[Jarvis]: {text}")
        self._voice_output.speak(text, blocking=True)

    async def say_farewell_async(self, text: str = "Shutting down. Goodbye.") -> None:
        await asyncio.to_thread(self.say_farewell, text)

    def stop(self) -> None:
        self._voice_output.stop()

    def _speak_streamed(self, response: str) -> None:
        sentences = split_sentences(response)
        for index, sentence in enumerate(sentences):
            self._voice_output.speak(sentence, blocking=index == len(sentences) - 1)
