from __future__ import annotations

from jarvis.config.settings import load_settings
from jarvis.observability import configure_logging


def setup_persistent_logging() -> None:
    configure_logging(load_settings())

