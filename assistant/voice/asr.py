import sounddevice as sd
import numpy as np
import whisper
import warnings
import threading
import logging
import os
import yaml
import queue
import time
from pathlib import Path
from scipy.signal import resample_poly
from math import gcd

# Ignore fp16 warnings if running on CPU
warnings.filterwarnings("ignore", category=UserWarning)

logger = logging.getLogger("Jarvis.ASR")
VOICE_DEBUG = os.environ.get("JARVIS_VOICE_DEBUG", "").strip() == "1"


def _console(message: str) -> None:
    print(message, flush=True)


def _debug_meter(message: str) -> None:
    if VOICE_DEBUG:
        print(message, end="\r", flush=True)

# ── Target sample rate for Whisper (always 16kHz) ──
WHISPER_SR = 16000


def load_config():
    config_path = Path(__file__).resolve().parent.parent / "config" / "models.yaml"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
            return config.get("audio", {})
    except Exception:
        return {}


def find_best_input_device():
    """
    Auto-detect the best microphone input device.

    Priority order:
      1. DirectSound microphone (shared-mode, works alongside TTS output)
      2. MME microphone (fallback, widely compatible)
      3. WASAPI microphone (exclusive-mode, conflicts with TTS)
      4. WDM-KS / others (often problematic)

    Skips loopback/stereo-mix/PC-speaker devices.
    """
    devices = sd.query_devices()
    skip_keywords = ["stereo mix", "pc speaker", "loopback", "output", "speaker",
                     "headphone"]

    candidates = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] < 1:
            continue
        name_lower = dev["name"].lower()
        if any(kw in name_lower for kw in skip_keywords):
            continue
        # Priority: DirectSound > MME > WASAPI > others
        # DirectSound uses shared-mode which is critical for simultaneous
        # mic input + speaker output (TTS). WASAPI exclusive mode locks
        # the audio subsystem and causes errors when TTS is playing.
        hostapi_name = sd.query_hostapis(dev["hostapi"])["name"].lower()
        if "directsound" in hostapi_name:
            priority = 0
        elif "mme" in hostapi_name:
            priority = 1
        elif "wasapi" in hostapi_name:
            priority = 2
        else:
            priority = 3
        candidates.append((priority, i, dev))

    if not candidates:
        _console("[ASR] WARNING: No input devices found, using system default")
        return None

    candidates.sort(key=lambda x: x[0])
    best_priority, best_id, best_dev = candidates[0]
    _console(
        f"[ASR] Selected mic: [{best_id}] {best_dev['name']} "
        f"(channels={best_dev['max_input_channels']}, "
        f"rate={best_dev['default_samplerate']:.0f}, "
        f"hostapi={sd.query_hostapis(best_dev['hostapi'])['name']})"
    )
    return best_id


def get_device_native_rate(device_id):
    """Get the native sample rate for a device. Returns int."""
    if device_id is None:
        return 44100
    dev = sd.query_devices(device_id)
    return int(dev["default_samplerate"])


def resample_audio(audio, orig_sr, target_sr):
    """Resample audio from orig_sr to target_sr using polyphase filtering."""
    if orig_sr == target_sr:
        return audio
    # Find the GCD for efficient rational resampling
    g = gcd(orig_sr, target_sr)
    up = target_sr // g
    down = orig_sr // g
    resampled = resample_poly(audio, up, down)
    return resampled.astype('float32')


# ── Load config and setup defaults ──
audio_config = load_config()
config_mic_id = audio_config.get("mic_device", None)
speaker_id = audio_config.get("speaker_device", 3)
default_fs = audio_config.get("sample_rate", 16000)

# VAD tuning parameters from config (with sensible defaults)
default_vad_threshold = audio_config.get("vad_threshold", 0.01)
default_silence_duration = audio_config.get("silence_duration", 1.5)
default_chunk_size = audio_config.get("chunk_size", 0.3)

# ── Mic selection: use config value if set, otherwise auto-detect ──
# Device selection logic moved to ASR.__init__ to avoid import-time side effects.
mic_id = None
device_native_rate = 44100

# Load model globally so it doesn't reload on every listen
# _console("Loading Whisper model ('small')...")
# model = whisper.load_model("small")  # Model is now lazy-loaded in ASR.model property


# ── Shared calibration function ──
def calibrate_noise(native_rate, duration=2.0):
    """
    Measure background noise for `duration` seconds and compute a stable
    VAD threshold.

    Records at the device's native sample rate for compatibility.
    Uses P95 of |audio| — this ignores quiet baseline AND random spikes,
    giving a stable "typical worst-case noise" value.

    Threshold is set to 1.5x P95 and clamped to [0.01, 0.35].
    """
    _console("[ASR] Calibrating background noise... stay silent")
    try:
        recording = sd.rec(int(duration * native_rate), samplerate=native_rate,
                           channels=1, dtype='float32')
        sd.wait()
        audio = np.squeeze(recording)

        # Compute noise stats
        noise_mean = np.mean(np.abs(audio))
        noise_rms = np.sqrt(np.mean(audio ** 2))
        noise_p95 = np.percentile(np.abs(audio), 95)
        noise_peak = np.max(np.abs(audio))

        _console(
            f"[ASR] Noise  mean={noise_mean:.5f}  rms={noise_rms:.5f}  "
            f"p95={noise_p95:.5f}  peak={noise_peak:.5f}"
        )

        # Use P95 * 1.5 as threshold — sits just above the noisy tail
        # Voice energy is typically 3-10x higher than background noise P95
        threshold = noise_p95 * 1.5
        threshold = max(threshold, 0.01)    # floor: prevents triggering on silence
        threshold = min(threshold, 0.35)    # ceiling: always allows loud speech

        _console(f"[ASR] Auto threshold: {threshold:.5f}")
        return threshold

    except Exception as e:
        logger.error(f"Calibration failed: {e}")
        fallback = max(default_vad_threshold, 0.02)
        _console(f"[ASR] Calibration failed, using fallback: {fallback:.5f}")
        return fallback


class ContinuousListener:
    """
    Always-on microphone listener that runs in a background thread.

    This is the core of the interrupt system. It continuously monitors
    the microphone for voice activity and:
      1. If TTS is speaking -> interrupts it immediately
      2. Captures the full utterance (speech -> silence)
      3. Delivers the audio to a queue for transcription

    The listener NEVER stops monitoring -- it runs for the entire
    lifetime of the application.
    """

    def __init__(self, tts=None, fs=16000, threshold=0.01,
                 silence_duration=1.5, chunk_size=0.3, native_rate=44100):
        self.tts = tts
        self.fs = fs                          # target rate for Whisper
        self.native_rate = native_rate         # actual recording rate
        self.threshold = threshold
        self.silence_duration = silence_duration
        self.chunk_size = chunk_size

        # Output queue: completed audio segments ready for transcription
        self._audio_queue = queue.Queue()

        # Control flags
        self._running = False
        self._thread = None
        self._interrupted = threading.Event()

        # Debounce: minimum time between interrupt triggers (ms)
        self._last_interrupt_time = 0.0
        self._interrupt_debounce_sec = 0.3  # 300ms debounce

        # Safety: max recording duration (seconds) to prevent infinite capture
        self._max_record_seconds = 30.0

    @property
    def interrupted(self):
        return self._interrupted.is_set()

    def clear_interrupt(self):
        self._interrupted.clear()

    def start(self):
        """Start the continuous background listener thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._listen_loop,
            daemon=True,
            name="ContinuousListener",
        )
        self._thread.start()
        logger.info("Continuous listener started")

    def stop(self):
        """Stop the listener thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        logger.info("Continuous listener stopped")

    def get_audio(self, timeout=None):
        """
        Block until a complete audio segment is available.

        Returns:
            tuple: (audio_np_array, was_interrupt) or (None, False) on timeout
        """
        try:
            return self._audio_queue.get(timeout=timeout)
        except queue.Empty:
            return None, False

    def _listen_loop(self):
        """
        Main listener loop -- runs continuously in background thread.

        State machine:
          IDLE -> waiting for speech (energy > threshold)
          RECORDING -> capturing audio until silence
          DELIVER -> push captured audio to queue
        """
        # Calibrate once at startup (uses native rate for recording)
        self.threshold = calibrate_noise(self.native_rate, duration=2.0)

        # Chunk size in samples at native rate
        chunk_samples = int(self.chunk_size * self.native_rate)
        silence_chunks_needed = int(self.silence_duration / self.chunk_size)

        while self._running:
            try:
                self._wait_and_capture(chunk_samples, silence_chunks_needed)
            except Exception as e:
                logger.error(f"Listener loop error: {e}")
                time.sleep(0.5)  # Prevent tight error loops

    def _wait_and_capture(self, chunk_samples, silence_chunks_needed):
        """
        Single capture cycle:
        1. Wait for voice activity
        2. Optionally interrupt TTS
        3. Record until silence
        4. Push audio to queue
        """
        # ── Phase 1: Wait for speech (low-latency polling) ──
        while self._running:
            try:
                # Optimized for performance: Use a specific blocksize to prevent overflows
                # and minimize overhead during continuous monitoring.
                chunk = sd.rec(chunk_samples, samplerate=self.native_rate,
                               channels=1, dtype='float32', blocking=True)
            except Exception as e:
                if "overflow" in str(e).lower():
                    logger.warning("Audio input overflow detected. Increasing buffer stability.")
                    time.sleep(0.1)
                    continue
                logger.error(f"Microphone error: {e}")
                time.sleep(0.5)
                continue

            chunk = np.squeeze(chunk)
            energy = np.max(np.abs(chunk))

            _debug_meter(f"  vol: {energy:.5f}  thr: {self.threshold:.5f}")

            if energy > self.threshold:
                _console(f"[VAD] Voice detected (vol={energy:.5f})")
                was_interrupt = False

                # ── Interrupt TTS if it's speaking ──
                if self.tts and self.tts.is_speaking:
                    now = time.time()
                    if now - self._last_interrupt_time > self._interrupt_debounce_sec:
                        _console("[VAD] User interrupted! Stopping TTS...")
                        self.tts.stop()
                        self._interrupted.set()
                        self._last_interrupt_time = now
                        was_interrupt = True
                    else:
                        # Too soon after last interrupt -- skip (debounce)
                        continue

                # Speech detected -> move to recording phase
                self._record_and_deliver(chunk, chunk_samples,
                                         silence_chunks_needed, was_interrupt)
                break  # Return to outer loop to start next cycle

    def _record_and_deliver(self, first_chunk, chunk_samples,
                            silence_chunks_needed, was_interrupt):
        """
        Phase 2 & 3: Record until silence, then deliver audio.
        Audio is recorded at native_rate and resampled to 16kHz before delivery.
        """
        _console("[VAD] Recording...")
        audio_chunks = [first_chunk]
        silent_count = 0
        max_chunks = int(self._max_record_seconds / self.chunk_size)
        chunk_count = 0

        while self._running:
            try:
                chunk = sd.rec(chunk_samples, samplerate=self.native_rate,
                               channels=1, dtype='float32')
                sd.wait()
            except Exception as e:
                logger.error(f"Microphone error during recording: {e}")
                break

            chunk = np.squeeze(chunk)
            energy = np.max(np.abs(chunk))
            audio_chunks.append(chunk)
            chunk_count += 1

            if energy < self.threshold:
                silent_count += 1
                if silent_count >= silence_chunks_needed:
                    _console("[VAD] Silence detected -> stopped recording")
                    break
            else:
                silent_count = 0

            # Safety: prevent infinite recording
            if chunk_count >= max_chunks:
                _console(f"[VAD] Max recording duration ({self._max_record_seconds}s) reached")
                break

        # ── Phase 3: Deliver captured audio ──
        if not audio_chunks:
            return

        audio = np.concatenate(audio_chunks)
        peak = np.max(np.abs(audio))

        if peak < self.threshold:
            _console("[VAD] No meaningful speech captured.")
            return

        # Normalize
        audio = (audio / peak).astype('float32')

        # Resample from native rate to 16kHz for Whisper
        audio = resample_audio(audio, self.native_rate, WHISPER_SR)

        # Push to queue for transcription
        self._audio_queue.put((audio, was_interrupt))


class ASR:
    """
    Automatic Speech Recognition with continuous listening support.

    Wraps ContinuousListener + Whisper transcription.
    Can operate in two modes:
      - Legacy mode: call listen() for one-shot recording (backward compat)
      - Continuous mode: start_continuous() for always-on listening
    """

    def __init__(self, tts=None):
        self._model = None
        self.fs = default_fs
        self.tts = tts
        
        # Initialize device settings (previously at module level)
        if config_mic_id is not None:
            self.mic_id = config_mic_id
            _console(f"[ASR] Using configured mic device: {self.mic_id}")
        else:
            self.mic_id = find_best_input_device()
            _console(f"[ASR] Auto-detected mic device: {self.mic_id}")

        self.native_rate = get_device_native_rate(self.mic_id)
        _console(f"[ASR] Device native rate: {self.native_rate} Hz  |  Whisper target: {WHISPER_SR} Hz")

        if self.mic_id is not None:
            sd.default.device = (self.mic_id, None)
        else:
            sd.default.device = (None, None)

        # Continuous listener instance
        self._listener = ContinuousListener(
            tts=tts,
            fs=default_fs,
            threshold=default_vad_threshold,
            silence_duration=default_silence_duration,
            chunk_size=default_chunk_size,
            native_rate=self.native_rate
        )

        # Shared state for interrupt signaling
        self._interrupted = threading.Event()

    @property
    def model(self):
        """Lazy-load the Whisper model on first access."""
        if self._model is None:
            _console("Loading Whisper model ('small')...")
            self._model = whisper.load_model("small")
        return self._model

    @property
    def interrupted(self):
        return self._listener.interrupted

    def clear_interrupt(self):
        self._listener.clear_interrupt()

    def start_continuous(self):
        """Start the always-on background listener."""
        self._listener.start()
        _console("[ASR] Always-on listener started")

    def stop_continuous(self):
        """Stop the background listener."""
        self._listener.stop()

    def listen_continuous(self):
        """
        Block until speech is captured by the background listener,
        then transcribe and return text.

        This is the primary method for the real-time voice loop.
        The background listener handles VAD and TTS interruption --
        this method just waits for audio and transcribes it.

        Returns:
            str: Transcribed text, or "" if nothing meaningful captured.
        """
        audio, was_interrupt = self._listener.get_audio(timeout=None)

        if audio is None:
            return ""

        # Transcribe (audio is already 16kHz from the listener)
        _console("[ASR] Recognizing...")
        result = self.model.transcribe(
            audio,
            fp16=False,
            language="en",
            no_speech_threshold=0.6,
            logprob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        if result.get("no_speech_prob", 0.0) > 0.6:
            return ""
        text = result["text"].strip()

        if text:
            _console(f"[ASR] Recognized: {text}")

        return text

    def listen(self):
        """
        Legacy one-shot listen (backward compatible).
        Records audio using energy-based VAD.
        If TTS is currently speaking and speech is detected, interrupts TTS first.
        Returns transcribed text.
        """
        return self.record_until_silence(
            silence_duration=default_silence_duration,
            chunk_size=default_chunk_size,
        )

    def record_until_silence(self, silence_duration=1.5, chunk_size=0.3):
        """
        Legacy Alexa-style voice capture with interrupt support:
        1. Calibrate noise dynamically
        2. Wait for speech (energy above threshold)
        3. If TTS is speaking -> interrupt it immediately
        4. Record while user speaks
        5. Stop when silence exceeds silence_duration seconds
        6. Resample + transcribe and return text
        """
        native_rate = self.native_rate

        # Dynamic threshold detection
        threshold = calibrate_noise(native_rate, duration=2)

        chunk_samples = int(chunk_size * native_rate)
        silence_chunks_needed = int(silence_duration / chunk_size)
        max_wait_chunks = int(60.0 / chunk_size)  # 60s max wait for speech

        # ── Phase 1: Wait for speech ──
        _console("[ASR] Listening... (speak now)")
        self._interrupted.clear()
        wait_count = 0

        while True:
            try:
                chunk = sd.rec(chunk_samples, samplerate=native_rate,
                               channels=1, dtype='float32')
                sd.wait()
            except Exception as e:
                logger.error(f"Microphone error: {e}")
                return ""

            chunk = np.squeeze(chunk)
            energy = np.max(np.abs(chunk))
            wait_count += 1

            _debug_meter(f"  vol: {energy:.5f}  thr: {threshold:.5f}")

            if energy > threshold:
                _console(f"[VAD] Voice detected (vol={energy:.5f})")
                # ── Interrupt TTS if it's speaking ──
                if self.tts and self.tts.is_speaking:
                    _console("[VAD] User interrupted! Stopping TTS...")
                    self.tts.stop()
                    self._interrupted.set()

                # Speech detected, start collecting
                break

            # Safety: don't wait forever
            if wait_count >= max_wait_chunks:
                _console("[ASR] Timed out waiting for speech")
                return ""

        # ── Phase 2: Record until silence ──
        _console("[VAD] Recording...")
        audio_chunks = [chunk]
        silent_count = 0
        max_record_chunks = int(30.0 / chunk_size)  # 30s max recording
        chunk_count = 0

        while True:
            try:
                chunk = sd.rec(chunk_samples, samplerate=native_rate,
                               channels=1, dtype='float32')
                sd.wait()
            except Exception as e:
                logger.error(f"Microphone error during recording: {e}")
                break

            chunk = np.squeeze(chunk)
            energy = np.max(np.abs(chunk))
            audio_chunks.append(chunk)
            chunk_count += 1

            if energy < threshold:
                silent_count += 1
                if silent_count >= silence_chunks_needed:
                    _console("[VAD] Silence detected -> stopped recording")
                    break
            else:
                silent_count = 0

            if chunk_count >= max_record_chunks:
                _console("[VAD] Max recording duration reached")
                break

        # ── Phase 3: Process audio ──
        if not audio_chunks:
            return ""

        audio = np.concatenate(audio_chunks)
        peak = np.max(np.abs(audio))
        if peak < threshold:
            _console("[ASR] No speech captured.")
            return ""

        audio = audio / peak
        audio = audio.astype('float32')

        # Resample from native rate to 16kHz for Whisper
        audio = resample_audio(audio, native_rate, WHISPER_SR)

        # ── Phase 4: Transcribe ──
        _console("[ASR] Recognizing...")
        result = self.model.transcribe(
            audio,
            fp16=False,
            language="en",
            no_speech_threshold=0.6,
            logprob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        if result.get("no_speech_prob", 0.0) > 0.6:
            return ""
        text = result["text"].strip()

        if text:
            _console(f"[ASR] Recognized: {text}")

        return text
