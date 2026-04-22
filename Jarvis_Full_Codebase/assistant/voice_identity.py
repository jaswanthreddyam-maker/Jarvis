"""Voice Identity — speaker verification security layer.

Uses speaker embeddings to authenticate the primary user vs guests.
All embeddings are stored locally — never sent to any API.

Modes:
    AUTHENTICATED → full command access
    GUEST → restricted commands only

Flow:
    voice → embedding → cosine similarity with stored profile → match score
    match > threshold → AUTHENTICATED
    else → GUEST
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("Jarvis.VoiceIdentity")

STORAGE_FILENAME = "voice_profile.json"
DEFAULT_THRESHOLD = 0.75
ENROLLMENT_SAMPLES_NEEDED = 3


class VoiceIdentity:
    """Speaker verification using cosine similarity on audio embeddings."""

    class Mode:
        AUTHENTICATED = "AUTHENTICATED"
        GUEST = "GUEST"
        UNKNOWN = "UNKNOWN"

    def __init__(self, storage_dir: Path | str | None = None) -> None:
        if storage_dir is None:
            storage_dir = Path(__file__).resolve().parents[1] / "memory"
        self._storage_path = Path(storage_dir) / STORAGE_FILENAME
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)

        self._profile_embedding: np.ndarray | None = None
        self._threshold = DEFAULT_THRESHOLD
        self._enrollment_samples: list[np.ndarray] = []
        self._is_enrolled = False
        self._current_mode = self.Mode.UNKNOWN
        self._last_verification_time = 0.0
        self._verification_cache_sec = 30.0  # Cache result for 30s
        self._lock = threading.Lock()

        self._extractor = None  # Lazy-loaded
        self._preprocess_wav = None
        self._load_profile()

    # ── Lazy model loading ───────────────────────────────────────────

    def _get_extractor(self):
        """Lazy-load the speaker embedding model."""
        if self._extractor is not None:
            return self._extractor

        try:
            from resemblyzer import VoiceEncoder, preprocess_wav
            self._extractor = VoiceEncoder("cpu")
            self._preprocess_wav = preprocess_wav
            logger.info("Resemblyzer VoiceEncoder loaded successfully")
        except ImportError:
            logger.warning(
                "resemblyzer not installed. Voice identity will use fallback. "
                "Install with: pip install resemblyzer"
            )
            self._extractor = _FallbackExtractor()
            self._preprocess_wav = None
        except Exception as e:
            logger.error("Failed to load VoiceEncoder: %s", e)
            self._extractor = _FallbackExtractor()
            self._preprocess_wav = None
        return self._extractor

    # ── Enrollment ───────────────────────────────────────────────────

    @property
    def is_enrolled(self) -> bool:
        return self._is_enrolled

    @property
    def current_mode(self) -> str:
        return self._current_mode

    @property
    def enrollment_progress(self) -> tuple[int, int]:
        """Returns (samples_collected, samples_needed)."""
        return len(self._enrollment_samples), ENROLLMENT_SAMPLES_NEEDED

    def add_enrollment_sample(self, audio: np.ndarray, sample_rate: int = 16000) -> bool:
        """Add a voice sample for enrollment.

        Returns True when enough samples have been collected and
        the profile is finalized.
        """
        embedding = self._extract_embedding(audio, sample_rate)
        if embedding is None:
            logger.warning("Failed to extract embedding from enrollment sample")
            return False

        with self._lock:
            self._enrollment_samples.append(embedding)
            logger.info(
                "Enrollment sample %d/%d collected",
                len(self._enrollment_samples), ENROLLMENT_SAMPLES_NEEDED,
            )

            if len(self._enrollment_samples) >= ENROLLMENT_SAMPLES_NEEDED:
                # Average all enrollment embeddings to create robust profile
                stacked = np.stack(self._enrollment_samples)
                self._profile_embedding = np.mean(stacked, axis=0)
                # Normalize
                norm = np.linalg.norm(self._profile_embedding)
                if norm > 0:
                    self._profile_embedding /= norm
                self._is_enrolled = True
                self._enrollment_samples.clear()
                self._save_profile()
                logger.info("Voice profile enrolled successfully!")
                return True
        return False

    def clear_enrollment(self) -> None:
        """Wipe the stored voice profile."""
        with self._lock:
            self._profile_embedding = None
            self._is_enrolled = False
            self._enrollment_samples.clear()
            self._current_mode = self.Mode.UNKNOWN
        if self._storage_path.exists():
            self._storage_path.unlink()
        logger.warning("Voice profile cleared.")

    # ── Verification ─────────────────────────────────────────────────

    def verify(self, audio: np.ndarray, sample_rate: int = 16000) -> tuple[str, float]:
        """Verify a voice sample against the enrolled profile.

        Returns:
            (mode, similarity_score)
            mode is AUTHENTICATED, GUEST, or UNKNOWN
        """
        if not self._is_enrolled:
            self._current_mode = self.Mode.UNKNOWN
            return self.Mode.UNKNOWN, 0.0

        # Use cached result if recent enough
        now = time.time()
        if now - self._last_verification_time < self._verification_cache_sec:
            return self._current_mode, 0.0

        embedding = self._extract_embedding(audio, sample_rate)
        if embedding is None:
            return self.Mode.GUEST, 0.0

        with self._lock:
            similarity = float(np.dot(self._profile_embedding, embedding))
            self._last_verification_time = now

            if similarity >= self._threshold:
                self._current_mode = self.Mode.AUTHENTICATED
                logger.info("Voice verified: AUTHENTICATED (score=%.3f)", similarity)
            else:
                self._current_mode = self.Mode.GUEST
                logger.info("Voice mismatch: GUEST (score=%.3f)", similarity)

            return self._current_mode, similarity

    def is_authenticated(self) -> bool:
        """Quick check: is the current session authenticated?"""
        if not self._is_enrolled:
            return True  # No profile = no restriction
        return self._current_mode == self.Mode.AUTHENTICATED

    def set_threshold(self, threshold: float) -> None:
        self._threshold = max(0.0, min(1.0, threshold))

    # ── Internal ─────────────────────────────────────────────────────

    def _extract_embedding(self, audio: np.ndarray, sample_rate: int) -> np.ndarray | None:
        """Extract a normalized speaker embedding from raw audio."""
        try:
            extractor = self._get_extractor()
            if isinstance(extractor, _FallbackExtractor):
                return extractor.embed(audio, sample_rate)

            # resemblyzer expects float32, mono, any sample rate
            if audio.dtype != np.float32:
                audio = audio.astype(np.float32)
            if audio.ndim > 1:
                audio = audio.mean(axis=1)

            # Resample to 16kHz if needed
            if sample_rate != 16000:
                from scipy.signal import resample_poly
                from math import gcd
                g = gcd(sample_rate, 16000)
                audio = resample_poly(audio, 16000 // g, sample_rate // g).astype(np.float32)

            if self._preprocess_wav:
                audio = self._preprocess_wav(audio)
            embedding = extractor.embed_utterance(audio)

            norm = np.linalg.norm(embedding)
            if norm > 0:
                embedding = embedding / norm
            return embedding

        except Exception as e:
            logger.error("Embedding extraction failed: %s", e)
            return None

    # ── Persistence ──────────────────────────────────────────────────

    def _save_profile(self) -> None:
        try:
            data: dict[str, Any] = {
                "threshold": self._threshold,
                "enrolled": self._is_enrolled,
            }
            if self._profile_embedding is not None:
                data["embedding"] = self._profile_embedding.tolist()
            with open(self._storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            logger.info("Voice profile saved to %s", self._storage_path)
        except Exception as e:
            logger.error("Failed to save voice profile: %s", e)

    def _load_profile(self) -> None:
        if not self._storage_path.exists():
            return
        try:
            with open(self._storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._threshold = data.get("threshold", DEFAULT_THRESHOLD)
            self._is_enrolled = data.get("enrolled", False)
            if "embedding" in data:
                self._profile_embedding = np.array(data["embedding"], dtype=np.float32)
            logger.info("Voice profile loaded (enrolled=%s)", self._is_enrolled)
        except Exception as e:
            logger.error("Failed to load voice profile: %s", e)


class _FallbackExtractor:
    """Fallback when resemblyzer is not installed.

    Uses a simple energy + spectral centroid fingerprint.
    Much less accurate but functional for basic identity.
    """

    EMBEDDING_DIM = 64

    def embed(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        # Split audio into segments and extract simple features
        n_segments = self.EMBEDDING_DIM // 2
        seg_len = max(1, len(audio) // n_segments)
        features = []

        for i in range(n_segments):
            seg = audio[i * seg_len:(i + 1) * seg_len]
            if len(seg) == 0:
                features.extend([0.0, 0.0])
                continue
            # RMS energy
            rms = float(np.sqrt(np.mean(seg ** 2)))
            # Zero-crossing rate
            zcr = float(np.mean(np.abs(np.diff(np.sign(seg))) > 0))
            features.extend([rms, zcr])

        embedding = np.array(features[:self.EMBEDDING_DIM], dtype=np.float32)
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding /= norm
        return embedding


# ── Singleton ────────────────────────────────────────────────────────
voice_id = VoiceIdentity()
