from __future__ import annotations

import threading
from typing import Callable


class TTS:
    def __init__(self) -> None:
        self.enabled = True
        self.output_device = None
        self._on_finished: Callable[[bool], None] | None = None
        self._on_started: Callable[[], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        try:
            import pyttsx3

            self._engine = pyttsx3.init()
        except Exception:
            self._engine = None
            self.enabled = False

    def set_on_finished(self, callback) -> None:
        self._on_finished = callback

    def set_on_started(self, callback) -> None:
        self._on_started = callback

    def speak(self, text: str, *, blocking: bool = False) -> None:
        self._stop_event.clear()
        if self._on_started is not None:
            self._on_started()
        if self._engine is None:
            if self._on_finished is not None:
                self._on_finished(False)
            return

        def _run() -> None:
            interrupted = False
            try:
                self._engine.say(text)
                self._engine.runAndWait()
                interrupted = self._stop_event.is_set()
            finally:
                if self._on_finished is not None:
                    self._on_finished(interrupted)

        if blocking:
            _run()
            return
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def wait(self) -> None:
        if self._thread is not None:
            self._thread.join()

    def stop(self) -> None:
        self._stop_event.set()
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                return

