from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from assistant.app import JarvisAssistant, build_assistant
from assistant.logger import setup_persistent_logging
from assistant.voice.asr import ASR
from assistant.voice.tts import TTS


def _speak_streamed(tts, response: str) -> None:
    """Split response into sentences and speak each one immediately,
    so the first sentence plays while the rest are still being processed."""
    sentences = re.split(r'(?<=[.!?])\s+', response.strip())
    for i, sentence in enumerate(sentences):
        sentence = sentence.strip()
        if not sentence:
            continue
        is_last = (i == len(sentences) - 1)
        tts.speak(sentence, blocking=is_last)


def _configure_espeak() -> None:
    if sys.platform != "win32":
        return
    if os.environ.get("PHONEMIZER_ESPEAK_LIBRARY"):
        return

    candidates = [
        Path(r"C:\Program Files\eSpeak NG\libespeak-ng.dll"),
        Path(r"C:\Program Files (x86)\eSpeak NG\libespeak-ng.dll"),
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = str(candidate)
        os.environ["PATH"] = str(candidate.parent) + os.pathsep + os.environ.get("PATH", "")
        break


def _assistant_memory_db_path() -> Path:
    return Path(__file__).resolve().parent / "assistant" / "memory" / "longterm.db"


def _handle_assistant_text(assistant: JarvisAssistant, text: str) -> str:
    assistant.begin_execution()
    try:
        response, _ = assistant.handle_text(text)
        return response
    finally:
        assistant.finish_execution()


def print_banner() -> None:
    print("=" * 50)
    print("      JARVIS LOCAL AI ASSISTANT")
    print("      Real-Time Interruptible Voice")
    print("=" * 50)


async def main() -> None:
    _configure_espeak()

    parser = argparse.ArgumentParser(description="Jarvis Local AI Assistant")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--voice", action="store_true", help="Enable voice input via ASR")
    parser.add_argument("--test-mode", action="store_true", help="Run a demo search")
    args = parser.parse_args()

    os.environ["JARVIS_DEBUG"] = "1" if args.debug else ""
    setup_persistent_logging()
    logger = logging.getLogger("Jarvis.Main")

    print_banner()
    logger.info("Starting Jarvis initialization...")

    assistant = build_assistant(memory_db_path=_assistant_memory_db_path())
    loop = asyncio.get_running_loop()

    if args.test_mode:
        logger.info("[Test Mode] Running demo automation...")
        response = await loop.run_in_executor(
            None,
            _handle_assistant_text,
            assistant,
            "search the web for 'python automation' and write the results to a file named 'demo_search.txt'",
        )
        print(f"[Jarvis]: {response}")
        return

    if args.voice:
        await run_voice_loop(assistant, logger)
    else:
        await run_text_loop(assistant, logger)

    print("Goodbye.")


async def run_voice_loop(assistant: JarvisAssistant, logger: logging.Logger) -> None:
    tts = TTS()
    asr = ASR(tts=tts)

    def on_tts_finished(interrupted: bool) -> None:
        if interrupted:
            assistant.cancel_active()

    tts.set_on_finished(on_tts_finished)
    asr.start_continuous()

    greeting = "Jarvis is online and ready."
    print(f"[Jarvis]: {greeting}")
    tts.speak(greeting)
    tts.wait()

    print("\nReal-time voice mode active. Speak anytime. Say 'exit' to quit.\n")

    loop = asyncio.get_running_loop()

    try:
        while True:
            text = await loop.run_in_executor(None, asr.listen_continuous)

            if not text or not text.strip():
                continue

            if asr.interrupted:
                print(f"[Interrupted -> New command]: {text}")
                asr.clear_interrupt()
            else:
                print(f"[You said]: {text}")

            if text.strip().lower() in ("exit", "quit", "goodbye", "shut down"):
                farewell = "Shutting down. Goodbye."
                print(f"[Jarvis]: {farewell}")
                tts.speak(farewell, blocking=True)
                break

            response = await loop.run_in_executor(None, _handle_assistant_text, assistant, text)
            if response:
                print(f"[Jarvis]: {response}")
                _speak_streamed(tts, response)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        asr.stop_continuous()
        tts.stop()
        logger.info("Voice loop terminated")


async def run_text_loop(assistant: JarvisAssistant, logger: logging.Logger) -> None:
    tts = TTS()
    greeting = "Jarvis is online and ready."
    print(f"[Jarvis]: {greeting}")
    tts.speak(greeting, blocking=True)

    print("\nType your commands. Type 'exit' to quit.\n")

    loop = asyncio.get_running_loop()

    try:
        while True:
            user_input = await loop.run_in_executor(None, input, "\n[Jarvis Input]> ")

            if user_input.strip().lower() in ("exit", "quit"):
                break

            if user_input.strip():
                response = await loop.run_in_executor(
                    None,
                    _handle_assistant_text,
                    assistant,
                    user_input,
                )
                if response:
                    print(f"[Jarvis]: {response}")
                    _speak_streamed(tts, response)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        tts.stop()
        logger.info("Text loop terminated")


if __name__ == "__main__":
    asyncio.run(main())
