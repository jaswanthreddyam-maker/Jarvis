from __future__ import annotations

import base64
import contextlib
import io
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger("Jarvis.TTS")
TTS_VERBOSE = os.environ.get("JARVIS_VERBOSE_TTS", "").strip() == "1"
COQUI_OPT_IN = os.environ.get("JARVIS_ENABLE_COQUI_TTS", "").strip().lower() in {"1", "true", "yes", "on"}


def _run_quietly(label: str, func, /, *args, **kwargs):
    if TTS_VERBOSE:
        return func(*args, **kwargs)

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
        result = func(*args, **kwargs)

    suppressed_stdout = stdout_buffer.getvalue().strip()
    suppressed_stderr = stderr_buffer.getvalue().strip()
    if suppressed_stdout:
        logger.debug("%s stdout suppressed", label)
    if suppressed_stderr:
        logger.debug("%s stderr suppressed", label)
    return result


def _tts_info(message: str) -> None:
    print(message, flush=True)


def _tts_debug(message: str) -> None:
    if TTS_VERBOSE:
        print(message, flush=True)


def _ensure_windows_runtime_dirs() -> None:
    """Provide stable writable dirs for Windows speech backends."""
    if os.name != "nt":
        return

    temp_root = Path(tempfile.gettempdir()) / "Jarvis"
    local_appdata = Path(os.environ.get("LOCALAPPDATA", temp_root / "LocalAppData"))
    appdata = Path(os.environ.get("APPDATA", temp_root / "AppData" / "Roaming"))

    local_appdata.mkdir(parents=True, exist_ok=True)
    appdata.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("LOCALAPPDATA", str(local_appdata))
    os.environ.setdefault("APPDATA", str(appdata))


def _windows_allows_coqui() -> bool:
    """Fast preflight so Windows startup does not stall on unavailable Coqui state."""
    if os.name != "nt":
        return True

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            local_appdata, _ = winreg.QueryValueEx(key, "Local AppData")
        return bool(local_appdata)
    except Exception as exc:
        logger.warning("Coqui preflight skipped on Windows: %s", exc)
        return False


def _validate_output_device(device_id: int | None) -> int | None:
    """Return a usable sounddevice output id or None for default routing."""
    try:
        import sounddevice as sd
    except ImportError:
        return None

    try:
        if device_id is None:
            default_device = sd.default.device
            if isinstance(default_device, (list, tuple)) and len(default_device) > 1:
                candidate = default_device[1]
                if candidate not in {None, -1}:
                    device_id = int(candidate)
                else:
                    return None
            else:
                return None

        device = sd.query_devices(device_id)
        if device.get("max_output_channels", 0) < 1:
            return None

        samplerate = int(device.get("default_samplerate", 44100) or 44100)
        sd.check_output_settings(
            device=device_id,
            channels=1,
            dtype="float32",
            samplerate=samplerate,
        )
        return int(device_id)
    except Exception as exc:
        logger.warning("Output device %s is not usable: %s", device_id, exc)
        return None


def find_best_output_device() -> tuple[int | None, str]:
    """
    Auto-detect the best speaker output device and validate it with sounddevice.
    """
    try:
        import sounddevice as sd
    except ImportError:
        return None, "System Default"

    devices = sd.query_devices()
    skip_keywords = [
        "headset",
        "hands-free",
        "bthhfenum",
        "primary sound",
        "sound mapper",
        "pc speaker",
    ]

    candidates: list[tuple[int, int, dict]] = []
    for index, device in enumerate(devices):
        if device.get("max_output_channels", 0) < 1:
            continue

        name_lower = str(device.get("name", "")).lower()
        if any(keyword in name_lower for keyword in skip_keywords):
            continue

        hostapi_name = sd.query_hostapis(device["hostapi"])["name"].lower()
        if "directsound" in hostapi_name:
            priority = 0
        elif "mme" in hostapi_name:
            priority = 1
        elif "wasapi" in hostapi_name:
            priority = 2
        else:
            priority = 3
        candidates.append((priority, index, device))

    candidates.sort(key=lambda item: item[0])
    for _, device_id, device in candidates:
        validated = _validate_output_device(device_id)
        if validated is None:
            continue
        api_name = sd.query_hostapis(device["hostapi"])["name"]
        description = f"{device['name']} ({api_name})"
        logger.info(
            "Selected TTS output device [%s] %s (channels=%s rate=%s)",
            validated,
            device["name"],
            device["max_output_channels"],
            f"{device['default_samplerate']:.0f}",
        )
        return validated, description

    logger.warning("No validated output devices found; using system default fallback")
    return _validate_output_device(None), "System Default"


_cached_device: tuple[int | None, str] | None = None

def get_best_output_device() -> tuple[int | None, str]:
    global _cached_device
    if _cached_device is None:
        _cached_device = find_best_output_device()
    return _cached_device


class TTS:
    """
    Interruptible text-to-speech with multiple playback backends.

    Backend order:
      1. Coqui TTS + sounddevice output
      2. winsound playback for generated WAV files
      3. Windows System.Speech via PowerShell
      4. Console/text fallback
    """

    def __init__(self):
        self.is_speaking = False
        self.is_generating = False
        self._stream = None
        self._lock = threading.Lock()
        self._playback_thread: threading.Thread | None = None
        self._system_voice_process: subprocess.Popen | None = None
        self._on_finished_callback = None
        self._on_started_callback = None
        self._stop_requested = False
        self._current_text = ""
        
        dev_id, dev_name = get_best_output_device()
        self.output_device = _validate_output_device(dev_id)
        self.output_device_name = dev_name
        
        self.backend = "text"
        self.enabled = False
        self.tts = None
        self.tts_sample_rate = 22050

        self._initialize_backend()

    def _initialize_backend(self) -> None:
        _ensure_windows_runtime_dirs()
        logger.info("Initializing TTS backend...")

        if os.environ.get("JARVIS_FORCE_MOCK_TTS") == "1":
            logger.info("MOCK_TTS: Forced by environment variable.")
            self.backend = "text"
            self.enabled = True
            return

        if os.name == "nt" and self._system_voice_supported() and not COQUI_OPT_IN:
            logger.info(
                "Using Windows system voice for fast startup. Set JARVIS_ENABLE_COQUI_TTS=1 to opt into Coqui."
            )
            self.backend = "system_voice"
            self.enabled = True
            _tts_info("[TTS] Voice ready: Windows system voice")
            return

        if os.name == "nt" and not _windows_allows_coqui():
            logger.info("Windows Coqui preflight failed; using system voice fallback.")
            if self._system_voice_supported():
                self.backend = "system_voice"
                self.enabled = True
                _tts_info("[TTS] Voice ready: Windows system voice")
                return

        try:
            logger.info("Importing Coqui TTS (heavy load)...")
            from TTS.api import TTS as CoquiTTS
            logger.info("Coqui TTS imported successfully.")

            self.tts = _run_quietly(
                "Coqui init",
                CoquiTTS,
                "tts_models/en/vctk/vits",
                gpu=False,
            )
            logger.info("Coqui model loaded.")
            self.tts_sample_rate = getattr(
                self.tts.synthesizer,
                "output_sample_rate",
                getattr(self.tts, "sample_rate", 22050),
            )
            self.backend = "coqui"
            self.enabled = True
            _tts_info(
                f"[TTS] Voice ready: Coqui ({self.tts_sample_rate} Hz) on {self.output_device_name}"
            )
            return
        except Exception as exc:
            logger.warning("Coqui TTS unavailable, falling back: %s", exc)
            _tts_info(f"[TTS] Coqui unavailable, falling back: {exc}")

        if self._system_voice_supported():
            self.backend = "system_voice"
            self.enabled = True
            _tts_info("[TTS] Voice ready: Windows system voice")
            return

        self.backend = "text"
        self.enabled = False
        _tts_info("[TTS] Voice ready: text-only fallback")

    def _system_voice_supported(self) -> bool:
        return os.name == "nt" and shutil.which("powershell") is not None

    def set_on_finished(self, callback):
        self._on_finished_callback = callback

    def set_on_started(self, callback):
        self._on_started_callback = callback

    def speak(self, text: str, blocking: bool = False):
        if not text.strip():
            self._finish_playback(False)
            return

        self._current_text = text

        if self.backend == "system_voice":
            self._speak_with_system_voice(text, blocking=blocking)
            return

        if not self.enabled or self.backend != "coqui":
            _tts_info(f"[TTS Output]: {text}")
            self._notify_started()
            self._finish_playback(False)
            return

        self.stop()

        with self._lock:
            self.is_generating = True
            self._stop_requested = False

        kwargs = {}
        if getattr(self.tts, "is_multi_speaker", False) and self.tts.speakers:
            kwargs["speaker"] = self.tts.speakers[0]

        output_file = os.path.join(tempfile.gettempdir(), "jarvis_tts_output.wav")

        try:
            _tts_debug("[TTS] Generating audio")
            self.tts.tts_to_file(text=text, file_path=output_file, **kwargs)
            _tts_debug(f"[TTS] Audio generated: {output_file}")
        except Exception as exc:
            logger.error("TTS generation error: %s", exc)
            _tts_info(f"[TTS ERROR] TTS generation error: {exc}")
            with self._lock:
                self.is_generating = False
            if self._system_voice_supported():
                self._speak_with_system_voice(text, blocking=blocking)
            else:
                self._finish_playback(False)
            return

        with self._lock:
            self.is_generating = False

        if not self._should_continue():
            self._finish_playback(True)
            return

        try:
            import numpy as np
            import soundfile as sf
        except ImportError:
            _tts_debug("[TTS] soundfile unavailable, using playback fallback")
            self._play_fallback(output_file, text=text)
            return

        try:
            data, fs = sf.read(output_file, dtype="float32")
            _tts_debug(
                f"[TTS] Audio loaded: shape={data.shape}, rate={fs}, "
                f"dtype={data.dtype}, duration={len(data)/fs:.2f}s"
            )

            if len(data.shape) > 1:
                data = np.mean(data, axis=1)
                _tts_debug(f"[TTS] Converted to mono: shape={data.shape}")

            peak = float(np.max(np.abs(data))) if len(data) else 0.0
            if peak > 0.0:
                data = (data / peak).astype("float32")
            else:
                data = data.astype("float32")

            if blocking:
                self._start_stream_playback(data, fs, output_file)
            else:
                self._playback_thread = threading.Thread(
                    target=self._start_stream_playback,
                    args=(data, fs, output_file),
                    daemon=True,
                )
                self._playback_thread.start()
        except Exception as exc:
            logger.error("TTS sounddevice load/play error: %s", exc)
            _tts_info(f"[TTS ERROR] sounddevice load/play failed: {exc}")
            self._play_fallback(output_file, text=text)

    def _should_continue(self) -> bool:
        with self._lock:
            return not self._stop_requested

    def _start_stream_playback(self, data, fs: int, wav_path: str | None = None):
        try:
            import sounddevice as sd
        except ImportError:
            self._play_fallback(wav_path, text=self._current_text)
            return

        frame_size = 1024
        total_frames = len(data)
        pos = 0
        handled_by_fallback = False

        with self._lock:
            self.is_speaking = True
            self._stop_requested = False

        device_to_use = _validate_output_device(self.output_device)
        self.output_device = device_to_use

        try:
            if device_to_use is not None:
                dev_info = sd.query_devices(device_to_use)
                dev_name = dev_info.get("name", "Unknown")
            else:
                dev_info = sd.query_devices(kind="output")
                dev_name = dev_info.get("name", "System Default")
            _tts_debug(f"[TTS] Output device: {dev_name}")
        except Exception:
            _tts_debug("[TTS] Output device: System Default")
            device_to_use = None

        self._notify_started()

        try:
            self._stream = sd.OutputStream(
                samplerate=fs,
                channels=1,
                dtype="float32",
                device=device_to_use,
            )
            self._stream.start()
            _tts_debug("[TTS] OutputStream started successfully")

            while pos < total_frames:
                with self._lock:
                    if not self.is_speaking:
                        break
                end = min(pos + frame_size, total_frames)
                chunk = data[pos:end]
                self._stream.write(chunk.reshape(-1, 1))
                pos = end

            if pos >= total_frames:
                _tts_debug("[TTS] Playback complete")
        except Exception as exc:
            logger.error("TTS sounddevice playback error: %s", exc)
            _tts_info(f"[TTS ERROR] Playback failed: {exc}")
            handled_by_fallback = True
            self._play_fallback(wav_path, text=self._current_text)
        finally:
            self._cleanup_stream()
            if not handled_by_fallback:
                interrupted = False
                with self._lock:
                    interrupted = self._stop_requested
                self._finish_playback(interrupted)

    def _play_fallback(self, wav_path: str | None, *, text: str = "") -> None:
        """Fallback playback using winsound or the Windows system voice."""
        with self._lock:
            self.is_speaking = True
            self._stop_requested = False

        self._notify_started()

        interrupted = False
        try:
            if os.name == "nt" and wav_path and os.path.exists(wav_path):
                import winsound

                _tts_debug(f"[TTS] Fallback playback via winsound: {wav_path}")
                winsound.PlaySound(wav_path, winsound.SND_FILENAME)
                _tts_debug("[TTS] Fallback playback completed")
            elif text and self._system_voice_supported():
                self._speak_with_system_voice(text, blocking=True, restart=False)
                return
            else:
                _tts_info(f"[TTS Output]: {text or wav_path or 'No audio available'}")
        except Exception as exc:
            logger.error("Fallback playback failed: %s", exc)
            _tts_info(f"[TTS ERROR] Fallback also failed: {exc}")
        finally:
            with self._lock:
                interrupted = self._stop_requested
            self._finish_playback(interrupted)

    def _speak_with_system_voice(
        self,
        text: str,
        *,
        blocking: bool,
        restart: bool = True,
    ) -> None:
        if restart:
            self.stop()

        with self._lock:
            self.is_speaking = True
            self.is_generating = False
            self._stop_requested = False

        self._notify_started()

        try:
            text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            script = (
                f"$bytes=[System.Convert]::FromBase64String('{text_b64}');"
                "$text=[System.Text.Encoding]::UTF8.GetString($bytes);"
                "Add-Type -AssemblyName System.Speech;"
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                "$s.SetOutputToDefaultAudioDevice();"
                "$s.Speak($text);"
            )
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-EncodedCommand", encoded],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._system_voice_process = process
        except Exception as exc:
            logger.error("System voice launch failed: %s", exc)
            _tts_info(f"[TTS ERROR] System voice launch failed: {exc}")
            self._finish_playback(False)
            return

        if blocking:
            try:
                process.wait()
            finally:
                interrupted = False
                with self._lock:
                    interrupted = self._stop_requested
                if self._system_voice_process is process:
                    self._system_voice_process = None
                self._finish_playback(interrupted)
            return

        self._playback_thread = threading.Thread(
            target=self._monitor_system_voice,
            args=(process,),
            daemon=True,
        )
        self._playback_thread.start()

    def _monitor_system_voice(self, process: subprocess.Popen) -> None:
        try:
            process.wait()
        finally:
            interrupted = False
            with self._lock:
                interrupted = self._stop_requested
            if self._system_voice_process is process:
                self._system_voice_process = None
            self._finish_playback(interrupted)

    def _notify_started(self) -> None:
        if self._on_started_callback:
            try:
                self._on_started_callback()
            except Exception as exc:
                logger.error("on_started callback error: %s", exc)

    def _finish_playback(self, interrupted: bool) -> None:
        with self._lock:
            self.is_speaking = False
            self.is_generating = False

        if self._on_finished_callback:
            try:
                self._on_finished_callback(interrupted=interrupted)
            except Exception as exc:
                logger.error("on_finished callback error: %s", exc)

    def stop(self):
        _tts_debug("[TTS] stop() called")
        with self._lock:
            self.is_speaking = False
            self._stop_requested = True

        self._cleanup_stream()

        process = self._system_voice_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

        if os.name == "nt":
            try:
                import winsound

                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass

        thread = self._playback_thread
        if (
            thread
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.0)
        self._playback_thread = None

    def _cleanup_stream(self):
        try:
            if self._stream is not None:
                if self._stream.active:
                    self._stream.stop()
                self._stream.close()
                self._stream = None
        except Exception:
            self._stream = None

    def wait(self):
        thread = self._playback_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join()

    @property
    def is_active(self):
        with self._lock:
            return self.is_speaking or self.is_generating
