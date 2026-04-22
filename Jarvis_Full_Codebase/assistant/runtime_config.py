from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "assistant" / "config" / "config.yaml"

_DEFAULT_AUDIO_CONFIG: dict[str, Any] = {
    "sample_rate": 16000,
    "vad_threshold": 0.01,
    "silence_duration": 1.5,
    "chunk_size": 0.3,
    "whisper_model": "tiny.en",
    "background_wake_enabled": False,
    "background_wake_interval_seconds": 1.75,
}


def load_runtime_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def merge_audio_config(raw_audio: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(_DEFAULT_AUDIO_CONFIG)
    if isinstance(raw_audio, dict):
        merged.update(raw_audio)
    return merged


def get_audio_config(path: Path | None = None) -> dict[str, Any]:
    config = load_runtime_config(path=path)
    audio = config.get("audio")
    return merge_audio_config(audio if isinstance(audio, dict) else None)
