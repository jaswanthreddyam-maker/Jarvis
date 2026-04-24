"""ASR Worker — persistent-subprocess Whisper transcription.

Architecture
~~~~~~~~~~~~
1.  ``_PersistentWhisper`` manages a long-lived child process that loads the
    Whisper model ONCE and processes transcription requests over a
    stdin/stdout JSON-line protocol (see ``_whisper_process.py``).

2.  ``_TranscribeThread`` is a lightweight QThread that saves audio to a temp
    file, sends a request to the persistent process, and waits for the
    response.  Because the child process is persistent, there is **zero**
    model-reload overhead per call — only I/O + inference time.

3.  ``ASRWorker`` is the public QObject interface consumed by the bridge.
    It exposes the same signals as before so nothing upstream changes.

Key fixes vs. the previous per-call subprocess model
----------------------------------------------------
*   Model loaded once → ~800 ms–3 s saved per transcription.
*   Bounded stream buffer (``deque(maxlen=100)``) prevents memory leak.
*   Wake-phrase deduplication (3 s cooldown) prevents double-fires.
*   Transcript noise filtering rejects filler phrases.
*   Structured ``[REQ-<id>]`` lifecycle logging.
*   Temp-file cleanup with retry for Windows PermissionError.
*   Cross-platform venv path resolution.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from uuid import uuid4

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal, Slot

from jarvis.interfaces.wake_word import contains_wake_phrase, extract_command_after_wake
from jarvis.runtime_config import get_audio_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Minimum character length for a transcript to be considered valid speech.
_MIN_TRANSCRIPT_LEN = 2

# Phrases that Whisper hallucinates on silence / noise.
_NOISE_PHRASES = frozenset({
    "", "you", "thanks", "thank you", "bye", "the end",
    "hmm", "uh", "um", "ah", "oh", "okay",
    "thanks for watching", "subscribe", "like and subscribe",
    "thank you for watching",
})


def _is_valid_transcript(text: str) -> bool:
    """Return False for empty, too-short, or hallucinated filler text."""
    cleaned = text.strip().lower().rstrip(".")
    if len(cleaned) < _MIN_TRANSCRIPT_LEN:
        return False
    if cleaned in _NOISE_PHRASES:
        return False
    return True


# ---------------------------------------------------------------------------
# Persistent Whisper subprocess manager
# ---------------------------------------------------------------------------

class _PersistentWhisper:
    """Manages a long-lived Whisper subprocess.

    The subprocess loads the model ONCE and processes transcription
    requests via stdin/stdout JSON-line protocol.  Access is serialised
    with a lock so only one request is in-flight at a time.
    """

    def __init__(self, model_name: str, python_exe: str) -> None:
        self._model_name = model_name
        self._python_exe = python_exe
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self, timeout: float = 120.0) -> dict:
        """Start the subprocess and block until the ready signal arrives."""
        script_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "_whisper_process.py",
        )

        self._proc = subprocess.Popen(
            [self._python_exe, script_path, self._model_name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
            env={
                **os.environ,
                "OMP_NUM_THREADS": "2",
                "MKL_NUM_THREADS": "2",
                "KMP_DUPLICATE_LIB_OK": "TRUE",
            },
        )

        ready_line = self._read_line(timeout=timeout)
        if ready_line is None:
            stderr = ""
            try:
                stderr = self._proc.stderr.read(500)
            except Exception:
                pass
            self.stop()
            raise TimeoutError(
                f"Whisper subprocess did not become ready within {timeout}s. "
                f"stderr: {stderr}"
            )

        ready_msg = json.loads(ready_line)
        if ready_msg.get("type") != "ready":
            self.stop()
            raise RuntimeError(f"Unexpected startup message: {ready_msg}")

        return ready_msg

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── transcription ────────────────────────────────────────────────

    def transcribe(
        self,
        audio_path: str,
        sample_rate: int,
        request_id: str,
        timeout: float = 30.0,
    ) -> dict:
        """Send a transcription request and wait for the matching response."""
        if not self.is_alive:
            raise RuntimeError("Whisper subprocess is not running")

        with self._lock:
            request = json.dumps({
                "id": request_id,
                "audio_path": audio_path,
                "sample_rate": sample_rate,
            })
            self._proc.stdin.write(request + "\n")
            self._proc.stdin.flush()

            # Read responses, matching by request ID to skip stale ones.
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Whisper transcription timed out (>{timeout}s)"
                    )
                line = self._read_line(timeout=remaining)
                if line is None:
                    raise TimeoutError(
                        f"Whisper transcription timed out (>{timeout}s)"
                    )
                response = json.loads(line)
                if response.get("id") == request_id:
                    return response
                # else: stale response from a previously-timed-out request

    # ── internal ─────────────────────────────────────────────────────

    def _read_line(self, timeout: float) -> str | None:
        """Read one line from subprocess stdout with a timeout.

        Uses a daemon thread because Windows does not support select()
        on pipe file-descriptors.
        """
        result: list[str | None] = [None]

        def _reader() -> None:
            try:
                line = self._proc.stdout.readline()
                if line:
                    result[0] = line.strip()
            except Exception:
                pass

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        reader.join(timeout)

        if reader.is_alive():
            return None  # timeout — thread will die when proc is killed
        return result[0]


# ---------------------------------------------------------------------------
# QThread wrapper for a single transcription request
# ---------------------------------------------------------------------------

class _TranscribeThread(QThread):
    """Sends one transcription request to the persistent Whisper process
    on a dedicated thread, keeping the Qt event-loop free.

    NOTE: Signal names must NOT shadow QThread.finished / QThread.started.
    """

    transcribe_done = Signal(int, str)    # request_id, transcript_text
    transcribe_error = Signal(int, str)   # request_id, error_message
    log = Signal(str, str)

    def __init__(
        self,
        request_id: int,
        audio: np.ndarray,
        sample_rate: int,
        whisper: _PersistentWhisper,
        parent=None,
    ):
        super().__init__(parent)
        self._request_id = request_id
        self._audio = audio
        self._sample_rate = sample_rate
        self._whisper = whisper

    def run(self) -> None:
        tmp_path: str | None = None
        req_tag = str(self._request_id)
        try:
            self.log.emit("ASR", f"[REQ-{req_tag}] ASR_START")

            # Save audio to temp file.
            tmp = tempfile.NamedTemporaryFile(suffix=".npy", delete=False)
            tmp_path = tmp.name
            np.save(tmp, self._audio)
            tmp.close()

            uid = uuid4().hex[:8]
            result = self._whisper.transcribe(
                audio_path=tmp_path,
                sample_rate=self._sample_rate,
                request_id=uid,
                timeout=30.0,
            )

            if result.get("type") == "error":
                msg = result.get("error", "Unknown subprocess error")
                self.log.emit("ASR", f"[REQ-{req_tag}] ASR_FAILED: {msg}")
                self.transcribe_error.emit(self._request_id, msg)
                return

            text = result.get("text", "").strip()
            elapsed = result.get("elapsed_ms", 0)
            self.log.emit("ASR", f"[REQ-{req_tag}] ASR_DONE ({elapsed:.0f}ms): {text}")
            self.transcribe_done.emit(self._request_id, text)

        except TimeoutError as exc:
            self.log.emit("ASR", f"[REQ-{req_tag}] ASR_TIMEOUT")
            self.transcribe_error.emit(self._request_id, str(exc))
        except Exception as exc:
            self.log.emit("ASR", f"[REQ-{req_tag}] ASR_ERROR: {exc}")
            self.transcribe_error.emit(self._request_id, f"Transcription error: {exc}")
        finally:
            # Subprocess normally cleans up the file, but belt-and-suspenders.
            if tmp_path:
                for _ in range(5):
                    try:
                        os.unlink(tmp_path)
                        break
                    except FileNotFoundError:
                        break
                    except PermissionError:
                        time.sleep(0.1)
                    except OSError:
                        break


# ---------------------------------------------------------------------------
# Public ASR Worker
# ---------------------------------------------------------------------------

class ASRWorker(QObject):
    ready = Signal(object)
    transcript_ready = Signal(int, str)
    transcript_failed = Signal(int, str)
    wake_detected = Signal(str)
    log = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        audio_config = get_audio_config()
        self._model_name = os.getenv(
            "JARVIS_WHISPER_MODEL",
            str(audio_config.get("whisper_model", "base.en")),
        )
        self._background_wake_enabled = _env_bool(
            "JARVIS_ENABLE_BACKGROUND_WAKE",
            bool(audio_config.get("background_wake_enabled", False)),
        )
        self._stream_decode_interval = float(
            os.getenv(
                "JARVIS_BACKGROUND_WAKE_INTERVAL",
                str(audio_config.get("background_wake_interval_seconds", 1.75)),
            )
        )

        # Bounded buffer prevents unbounded memory growth.
        self._stream_buffer: deque[np.ndarray] = deque(maxlen=100)
        self._is_transcribing = False
        self._last_stream_decode_at = 0.0
        self._active_thread: _TranscribeThread | None = None

        # Wake-phrase deduplication.
        self._last_wake_text: str = ""
        self._last_wake_time: float = 0.0

        # Persistent subprocess (started in load_model).
        self._whisper: _PersistentWhisper | None = None

        # Resolve venv python (cross-platform).
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        if sys.platform == "win32":
            venv_python = os.path.join(project_root, ".venv", "Scripts", "python.exe")
        else:
            venv_python = os.path.join(project_root, ".venv", "bin", "python")
        self._python_exe = venv_python if os.path.isfile(venv_python) else sys.executable

    # ── public slots ─────────────────────────────────────────────────

    @Slot()
    def load_model(self) -> None:
        """Start the persistent Whisper subprocess."""
        started_at = time.perf_counter()
        try:
            # Kill any previous subprocess.
            if self._whisper is not None:
                self._whisper.stop()

            self._whisper = _PersistentWhisper(self._model_name, self._python_exe)
            ready_msg = self._whisper.start(timeout=120.0)

            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            model = ready_msg.get("model", self._model_name)
            self.ready.emit({"status": "Ready", "model": model})
            self.log.emit("ASR", f"Whisper model '{model}' ready (persistent subprocess).")
            self.log.emit("Timing", f"ASR init ({model}): {elapsed_ms:.1f} ms")

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            self.log.emit("ASR", f"Whisper init failed ({elapsed_ms:.0f}ms): {exc}")
            self.ready.emit({
                "status": "Error",
                "model": self._model_name,
                "error": str(exc),
            })

    @Slot(bool)
    def set_background_wake_enabled(self, enabled: bool) -> None:
        self._background_wake_enabled = bool(enabled)
        if not enabled:
            self._stream_buffer.clear()
        state = "enabled" if enabled else "disabled"
        self.log.emit("ASR", f"Background wake transcription {state}.")

    @Slot(object)
    def on_audio_captured(self, chunk: object) -> None:
        try:
            if not self._background_wake_enabled:
                return
            self._stream_buffer.append(np.asarray(chunk, dtype=np.float32))

            if len(self._stream_buffer) >= 50 and not self._is_transcribing:
                now = time.perf_counter()
                if now - self._last_stream_decode_at < self._stream_decode_interval:
                    while len(self._stream_buffer) > 25:
                        self._stream_buffer.popleft()
                    return
                self._last_stream_decode_at = now
                self._is_transcribing = True
                audio = np.concatenate(list(self._stream_buffer))
                while len(self._stream_buffer) > 25:
                    self._stream_buffer.popleft()
                self._process_stream(audio)
        except Exception as exc:
            self.log.emit("ASR", f"[STREAM] error: {exc}")

    @Slot(object, int)
    def transcribe(self, payload: object, request_id: int) -> None:
        """Transcribe a captured utterance (manual/session mode)."""
        try:
            if isinstance(payload, dict):
                data = payload
            else:
                data = {"audio": payload, "sample_rate": 16000}
            audio = np.asarray(data.get("audio", []), dtype=np.float32).flatten()
            sample_rate = int(data.get("sample_rate", 16000))
            if audio.size == 0:
                self.transcript_ready.emit(request_id, "")
                return

            duration = audio.size / sample_rate
            self.log.emit("ASR", f"[REQ-{request_id}] audio received ({duration:.2f}s)")

            if self._whisper is None or not self._whisper.is_alive:
                self.transcript_failed.emit(request_id, "Whisper subprocess is not running")
                return

            thread = _TranscribeThread(
                request_id=request_id,
                audio=audio,
                sample_rate=sample_rate,
                whisper=self._whisper,
                parent=self,
            )
            thread.transcribe_done.connect(self._on_transcribe_done)
            thread.transcribe_error.connect(self.transcript_failed)
            thread.log.connect(self.log)
            thread.finished.connect(thread.deleteLater)
            self._active_thread = thread
            thread.start()

        except Exception as exc:
            self.transcript_failed.emit(request_id, f"Transcription failed: {exc}")

    # ── internal ─────────────────────────────────────────────────────

    def _on_transcribe_done(self, request_id: int, text: str) -> None:
        """Post-process transcript before forwarding to the bridge."""
        if not _is_valid_transcript(text):
            self.log.emit("ASR", f"[REQ-{request_id}] filtered noise: '{text}'")
            self.transcript_ready.emit(request_id, "")
            return
        self.transcript_ready.emit(request_id, text)

    def _process_stream(self, audio: object) -> None:
        """Background wake-word detection."""
        if self._whisper is None or not self._whisper.is_alive:
            self._is_transcribing = False
            return
        try:
            audio_np = np.asarray(audio, dtype=np.float32)
            thread = _TranscribeThread(
                request_id=-1,
                audio=audio_np,
                sample_rate=16000,
                whisper=self._whisper,
                parent=self,
            )
            thread.transcribe_done.connect(self._on_stream_result)
            thread.transcribe_error.connect(self._on_stream_failed)
            thread.log.connect(self.log)
            thread.finished.connect(thread.deleteLater)
            thread.start()
        except Exception as exc:
            self.log.emit("ASR", f"[STREAM] process error: {exc}")
            self._is_transcribing = False

    def _on_stream_result(self, _request_id: int, text: str) -> None:
        self._is_transcribing = False
        text = text.strip().lower()
        if not _is_valid_transcript(text):
            return
        self.log.emit("ASR_LOOP", f"transcription: {text}")
        if contains_wake_phrase(text):
            command = extract_command_after_wake(text)
            now = time.perf_counter()
            if (command == self._last_wake_text
                    and now - self._last_wake_time < 3.0):
                self.log.emit("ASR", "[WAKE] suppressed duplicate")
                return
            self._last_wake_text = command
            self._last_wake_time = now
            self.log.emit("ASR", "[WAKE] detected wake phrase")
            self.wake_detected.emit(command)

    def _on_stream_failed(self, _request_id: int, message: str) -> None:
        self._is_transcribing = False
        self.log.emit("ASR", f"[STREAM] error: {message}")
