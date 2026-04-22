"""Persistent Logging System — centralizes logging with rotation.

Provides file-based logging with automatic log rotation.
Stores errors and performance metrics persistently.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os

from assistant.event_bus import bus, Events

def setup_persistent_logging(log_dir: Path | str | None = None) -> None:
    if log_dir is None:
        log_dir = Path(__file__).resolve().parents[1] / "logs"
    else:
        log_dir = Path(log_dir)
        
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Main System Log (10 MB max, keep 5 backups)
    main_log = log_dir / "jarvis.log"
    main_handler = RotatingFileHandler(
        main_log, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    main_formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
    )
    main_handler.setFormatter(main_formatter)
    
    # 2. Metrics & Errors Log (separated for easy analysis)
    metrics_log = log_dir / "metrics_and_errors.log"
    metrics_handler = RotatingFileHandler(
        metrics_log, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    metrics_handler.setFormatter(main_formatter)

    # Configure root logger
    root_logger = logging.getLogger("Jarvis")
    root_logger.setLevel(logging.DEBUG if os.environ.get("JARVIS_DEBUG") else logging.INFO)
    root_logger.addHandler(main_handler)

    metrics_logger = logging.getLogger("Jarvis.Metrics")
    metrics_logger.setLevel(logging.INFO)
    metrics_logger.propagate = False
    metrics_logger.addHandler(metrics_handler)
    
    error_logger = logging.getLogger("Jarvis.Errors")
    error_logger.setLevel(logging.WARNING)
    error_logger.propagate = False
    error_logger.addHandler(metrics_handler)
    error_logger.addHandler(main_handler) # Also copy errors to main

    # Bridge EventBus to persistent logs
    def _on_error_occurred(msg: str):
        error_logger.error("SYSTEM ERROR OCCURRED: %s", msg)
        
    def _on_metrics_updated(metrics: dict):
        metrics_logger.info("Metrics: %s", metrics)
        
    bus.subscribe(Events.ERROR_OCCURRED, _on_error_occurred)
    bus.subscribe(Events.METRICS_UPDATED, _on_metrics_updated)

    root_logger.info("Persistent logging initialized at %s", log_dir)
