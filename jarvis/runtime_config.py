from __future__ import annotations

from jarvis.config.settings import load_settings


def get_audio_config() -> dict[str, object]:
    return dict(load_settings().audio)

