from __future__ import annotations

import logging
import threading
from typing import Callable


logger = logging.getLogger("Jarvis.TTS")


class TTS:
    """Text-to-speech wrapper around pyttsx3 (Windows SAPI5).

    IMPORTANT: On Windows, pyttsx3 creates COM (SAPI) objects.  COM objects
    created in a Single-Threaded Apartment **must** be used from the SAME
    thread that created them.  The previous implementation spawned a daemon
    thread for ``speak()`` which silently broke COM – speech never played.

    The fix: ``speak()`` now runs **blocking** on the calling thread.
    Since ``TTSWorker`` already lives on its own ``QThread``, blocking that
    thread is perfectly fine – the Qt main thread stays responsive.
    """

    def __init__(self) -> None:
        self.enabled = True
        self.output_device = None
        self._on_finished: Callable[[bool], None] | None = None
        self._on_started: Callable[[], None] | None = None
        self._stop_event = threading.Event()
        self._engine = None
        try:
            import pyttsx3

            self._engine = pyttsx3.init()
            logger.info("pyttsx3 engine initialised (SAPI5).")
        except Exception as exc:
            logger.warning("pyttsx3 init failed: %s", exc)
            self._engine = None
            self.enabled = False

    def set_on_finished(self, callback) -> None:
        self._on_finished = callback

    def set_on_started(self, callback) -> None:
        self._on_started = callback

    def speak(self, text: str, *, blocking: bool = True) -> None:
        """Speak *text* using the system voice.

        Always runs on the **current** thread to respect COM apartment
        threading rules.  The ``blocking`` kwarg is kept for API compat
        but is now always treated as ``True``.
        """
        self._stop_event.clear()

        # Check for stop before we even start — handles rapid stop→speak races.
        if self._stop_event.is_set():
            if self._on_finished is not None:
                try:
                    self._on_finished(True)
                except Exception:
                    pass
            return

        if self._on_started is not None:
            try:
                self._on_started()
            except Exception:
                pass

        if self._engine is None:
            if self._on_finished is not None:
                self._on_finished(False)
            return

        interrupted = False
        try:
            self._engine.say(text)
            self._engine.runAndWait()
        except RuntimeError:
            # pyttsx3 raises RuntimeError("run loop already started") when
            # stop() is called during runAndWait().  This is the expected
            # interruption path on Windows SAPI5.
            interrupted = True
        except Exception as exc:
            logger.warning("pyttsx3 speak error: %s", exc)
        finally:
            # Check the stop event AFTER runAndWait returns — if stop() was
            # called during playback, the event will be set even if
            # runAndWait() returned normally (e.g. SAPI finished before we
            # could check).
            if self._stop_event.is_set():
                interrupted = True
            if self._on_finished is not None:
                try:
                    self._on_finished(interrupted)
                except Exception:
                    pass

    def wait(self) -> None:
        # No-op — speak() is now always blocking on the calling thread.
        pass

    def stop(self) -> None:
        self._stop_event.set()
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                return
