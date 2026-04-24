from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections import deque

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal, Slot

from jarvis.interfaces.wake_word import contains_wake_phrase, extract_command_after_wake
from jarvis.runtime_config import get_audio_config


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Subprocess-based Whisper transcription
# ---------------------------------------------------------------------------
# Running whisper in a subprocess completely isolates it from the Qt UI
# process.  This prevents:
#   1. GIL contention (whisper's Python-level decode loops hold the GIL
#      and starve the Qt event-loop, freezing the UI).
#   2. PyTorch saturating all CPU cores (even with thread-limits, the OS
#      scheduler still deprioritises the UI when CPU is pegged).
#   3. Hard crashes from torch.set_num_interop_threads() conflicting with
#      already-initialised parallel work.
# ---------------------------------------------------------------------------

_WHISPER_SUBPROCESS_SCRIPT = r'''
import json, os, sys
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")

import numpy as np

def main():
    request = json.loads(sys.argv[1])
    model_name = request["model"]
    audio_path = request["audio_path"]
    sample_rate = request["sample_rate"]

    import torch
    torch.set_num_threads(2)
    import whisper

    audio = np.load(audio_path).astype(np.float32)
    if sample_rate != 16000 and audio.size > 0:
        duration = audio.size / float(sample_rate)
        target_size = max(1, int(duration * 16000))
        old_p = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
        new_p = np.linspace(0.0, 1.0, num=target_size, endpoint=False)
        audio = np.interp(new_p, old_p, audio).astype(np.float32)

    model = whisper.load_model(model_name)
    result = model.transcribe(audio, fp16=False)
    text = result.get("text", "").strip()
    print(json.dumps({"text": text}), flush=True)

if __name__ == "__main__":
    main()
'''


class _TranscribeThread(QThread):
    """Runs whisper in a subprocess on a dedicated thread so the Qt
    event-loop is never blocked.

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
        model_name: str,
        python_exe: str,
        parent=None,
    ):
        super().__init__(parent)
        self._request_id = request_id
        self._audio = audio
        self._sample_rate = sample_rate
        self._model_name = model_name
        self._python_exe = python_exe

    def run(self) -> None:
        tmp_audio_path: str | None = None
        tmp_script_path: str | None = None
        try:
            started = time.perf_counter()
            # Save audio to a temp file so the subprocess can read it.
            tmp_audio = tempfile.NamedTemporaryFile(
                suffix=".npy", delete=False
            )
            tmp_audio_path = tmp_audio.name
            np.save(tmp_audio, self._audio)
            tmp_audio.close()

            # Write the subprocess script to a temp file.
            tmp_script = tempfile.NamedTemporaryFile(
                suffix=".py", mode="w", delete=False, encoding="utf-8"
            )
            tmp_script_path = tmp_script.name
            tmp_script.write(_WHISPER_SUBPROCESS_SCRIPT)
            tmp_script.close()

            request_payload = json.dumps({
                "model": self._model_name,
                "audio_path": tmp_audio_path,
                "sample_rate": self._sample_rate,
            })

            self.log.emit("ASR", "[ASR] transcribing in subprocess...")

            proc = subprocess.run(
                [self._python_exe, tmp_script_path, request_payload],
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    "OMP_NUM_THREADS": "2",
                    "MKL_NUM_THREADS": "2",
                    "KMP_DUPLICATE_LIB_OK": "TRUE",
                },
            )

            elapsed = (time.perf_counter() - started) * 1000.0

            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()[-500:]
                self.transcribe_error.emit(
                    self._request_id,
                    f"Whisper subprocess failed (rc={proc.returncode}): {stderr}",
                )
                return

            stdout = (proc.stdout or "").strip()
            if not stdout:
                self.transcribe_error.emit(self._request_id, "Whisper subprocess produced no output.")
                return

            result = json.loads(stdout)
            text = result.get("text", "").strip()
            self.log.emit("ASR", f"[ASR] transcription ({elapsed:.0f}ms): {text}")
            self.transcribe_done.emit(self._request_id, text)

        except subprocess.TimeoutExpired:
            self.transcribe_error.emit(self._request_id, "Whisper transcription timed out (>30s).")
        except Exception as exc:
            self.transcribe_error.emit(self._request_id, f"Transcription error: {exc}")
        finally:
            # Clean up temp files with retry for Windows PermissionError
            # (subprocess may still hold the file handle briefly).
            for path in (tmp_audio_path, tmp_script_path):
                if not path:
                    continue
                for _attempt in range(5):
                    try:
                        os.unlink(path)
                        break
                    except PermissionError:
                        time.sleep(0.1)  # subprocess still reading
                    except OSError:
                        break


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
            str(audio_config.get("whisper_model", "tiny.en")),
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
        # Use a bounded deque to prevent unbounded memory growth during
        # long background-wake sessions.
        self._stream_buffer: deque[np.ndarray] = deque(maxlen=100)
        self._is_transcribing = False
        self._last_stream_decode_at = 0.0
        self._active_thread: _TranscribeThread | None = None

        # Wake phrase deduplication state.
        self._last_wake_text: str = ""
        self._last_wake_time: float = 0.0

        # Resolve the python executable from the project venv.
        # Works on both Windows (Scripts/python.exe) and Unix (bin/python).
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        if sys.platform == "win32":
            venv_python = os.path.join(project_root, ".venv", "Scripts", "python.exe")
        else:
            venv_python = os.path.join(project_root, ".venv", "bin", "python")
        self._python_exe = venv_python if os.path.isfile(venv_python) else sys.executable

    @Slot()
    def load_model(self) -> None:
        """Validate that whisper is importable (model itself is loaded
        on-demand in the subprocess)."""
        started_at = time.perf_counter()
        try:
            # Quick check that whisper is importable.
            import whisper  # noqa: F401
            self.ready.emit({"status": "Ready", "model": self._model_name})
            self.log.emit("ASR", f"Whisper model '{self._model_name}' ready (subprocess mode).")
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            self.log.emit("Timing", f"ASR init ({self._model_name}): {elapsed_ms:.1f} ms")
        except Exception as exc:
            self.ready.emit({"status": "Error", "model": self._model_name, "error": str(exc)})

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
                    # Trim buffer to most recent 25 chunks (deque handles max, but
                    # we still want to keep it tight for decode windows).
                    while len(self._stream_buffer) > 25:
                        self._stream_buffer.popleft()
                    return
                self._last_stream_decode_at = now
                self._is_transcribing = True
                audio = np.concatenate(list(self._stream_buffer))
                # Keep only recent chunks for next decode window.
                while len(self._stream_buffer) > 25:
                    self._stream_buffer.popleft()
                self._process_stream(audio)
        except Exception as exc:
            print(f"[ASR] Stream error: {exc}")

    def _process_stream(self, audio: object) -> None:
        """Background wake-word detection — uses subprocess too."""
        try:
            audio_np = np.asarray(audio, dtype=np.float32)
            thread = _TranscribeThread(
                request_id=-1,
                audio=audio_np,
                sample_rate=16000,
                model_name=self._model_name,
                python_exe=self._python_exe,
                parent=self,
            )
            thread.transcribe_done.connect(self._on_stream_result)
            thread.transcribe_error.connect(self._on_stream_failed)
            thread.log.connect(self.log)
            thread.finished.connect(thread.deleteLater)
            thread.start()
        except Exception as exc:
            self.log.emit("ASR", f"Stream process error: {exc}")
            self._is_transcribing = False

    def _on_stream_result(self, _request_id: int, text: str) -> None:
        self._is_transcribing = False
        text = text.strip().lower()
        if text:
            self.log.emit("ASR_LOOP", f"transcription: {text}")
            if contains_wake_phrase(text):
                command = extract_command_after_wake(text)
                # Deduplicate: don't fire wake_detected if the same text
                # was emitted within the last 3 seconds.
                now = time.perf_counter()
                if (command == self._last_wake_text
                        and now - self._last_wake_time < 3.0):
                    self.log.emit("ASR", "[ASR WAKE] suppressed duplicate wake phrase")
                    return
                self._last_wake_text = command
                self._last_wake_time = now
                self.log.emit("ASR", "[ASR WAKE] detected wake phrase")
                self.wake_detected.emit(command)

    def _on_stream_failed(self, _request_id: int, message: str) -> None:
        self._is_transcribing = False
        self.log.emit("ASR", f"Stream process error: {message}")

    @Slot(object, int)
    def transcribe(self, payload: object, request_id: int) -> None:
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

            self.log.emit("ASR", f"[ASR] audio received (length: {audio.size/sample_rate:.2f}s)")

            # Launch subprocess transcription on a dedicated thread.
            thread = _TranscribeThread(
                request_id=request_id,
                audio=audio,
                sample_rate=sample_rate,
                model_name=self._model_name,
                python_exe=self._python_exe,
                parent=self,
            )
            thread.transcribe_done.connect(self.transcript_ready)
            thread.transcribe_error.connect(self.transcript_failed)
            thread.log.connect(self.log)
            # Clean up thread when done (use QThread.finished, not our custom signal).
            thread.finished.connect(thread.deleteLater)
            self._active_thread = thread
            thread.start()

        except Exception as exc:
            self.transcript_failed.emit(request_id, f"Transcription failed: {exc}")
