"""Persistent Whisper transcription subprocess.

This script is launched ONCE by the ASRWorker and stays alive for the
entire application session.  It loads the Whisper model on startup,
then processes transcription requests via a JSON-line protocol on
stdin/stdout.

Protocol
--------
→ stdin  (one JSON object per line):
    {"id": "<uuid>", "audio_path": "/tmp/audio.npy", "sample_rate": 16000}

← stdout (one JSON object per line):
    Startup:  {"type": "ready", "model": "base.en"}
    Success:  {"type": "result", "id": "<uuid>", "text": "...", "elapsed_ms": 1234.5}
    Failure:  {"type": "error",  "id": "<uuid>", "error": "..."}

The subprocess cleans up each audio file after processing.
"""
from __future__ import annotations

import json
import os
import sys
import time

# Limit thread counts BEFORE importing torch/numpy.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import numpy as np  # noqa: E402


def _resample(audio: np.ndarray, src_rate: int, dst_rate: int = 16000) -> np.ndarray:
    if src_rate == dst_rate or audio.size == 0:
        return audio
    duration = audio.size / float(src_rate)
    target_size = max(1, int(duration * dst_rate))
    old_p = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    new_p = np.linspace(0.0, 1.0, num=target_size, endpoint=False)
    return np.interp(new_p, old_p, audio).astype(np.float32)


def main() -> None:
    model_name = sys.argv[1] if len(sys.argv) > 1 else "base.en"

    import torch
    torch.set_num_threads(2)
    import whisper  # noqa: E402

    # ── Load model ONCE ──────────────────────────────────────────────
    model = whisper.load_model(model_name)

    # Warm-up pass so first real request isn't slow.
    _dummy = np.zeros(16000, dtype=np.float32)
    model.transcribe(_dummy, fp16=False)

    # Signal ready.
    print(json.dumps({"type": "ready", "model": model_name}), flush=True)

    # ── Request loop ─────────────────────────────────────────────────
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        req_id = "unknown"
        try:
            request = json.loads(line)
            req_id = request.get("id", "unknown")
            audio_path = request["audio_path"]
            sample_rate = int(request.get("sample_rate", 16000))

            started = time.perf_counter()
            audio = np.load(audio_path).astype(np.float32)
            audio = _resample(audio, sample_rate)

            result = model.transcribe(audio, fp16=False)
            text = result.get("text", "").strip()
            elapsed_ms = (time.perf_counter() - started) * 1000

            print(json.dumps({
                "type": "result",
                "id": req_id,
                "text": text,
                "elapsed_ms": round(elapsed_ms, 1),
            }), flush=True)

        except Exception as exc:
            print(json.dumps({
                "type": "error",
                "id": req_id,
                "error": str(exc),
            }), flush=True)
        finally:
            # Best-effort cleanup of the audio file.
            try:
                ap = request.get("audio_path") if 'request' in dir() else None  # noqa
            except Exception:
                ap = None
            if ap:
                for _ in range(3):
                    try:
                        os.unlink(ap)
                        break
                    except PermissionError:
                        time.sleep(0.05)
                    except OSError:
                        break


if __name__ == "__main__":
    main()
