from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from config.settings import Settings


_STANDARD_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__.keys())


def safe_log_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        compact = " ".join(value.split())
        return compact if len(compact) <= 1200 else compact[:1197] + "..."
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): safe_log_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe_log_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return safe_log_value(vars(value))
    return repr(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = {
            key: safe_log_value(value)
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_FIELDS and not key.startswith("_")
        }
        if extras:
            payload["context"] = extras
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def configure_logging(settings: Settings, *, log_dir_override: Path | None = None) -> None:
    log_directory = (log_dir_override or settings.logging.directory).resolve()
    log_directory.mkdir(parents=True, exist_ok=True)

    application_log = log_directory / settings.logging.app_log_path.name
    error_log = log_directory / settings.logging.error_log_path.name

    root_logger = logging.getLogger()
    for handler in tuple(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    root_logger.setLevel(getattr(logging, settings.logging.level.upper(), logging.INFO))

    json_formatter = JsonFormatter()
    console_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    app_handler = RotatingFileHandler(
        application_log,
        maxBytes=settings.logging.max_bytes,
        backupCount=settings.logging.backup_count,
        encoding="utf-8",
    )
    app_handler.setLevel(logging.DEBUG)
    app_handler.setFormatter(json_formatter)
    root_logger.addHandler(app_handler)

    error_handler = RotatingFileHandler(
        error_log,
        maxBytes=settings.logging.max_bytes,
        backupCount=settings.logging.backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(json_formatter)
    root_logger.addHandler(error_handler)

    if settings.logging.console_enabled:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, settings.logging.level.upper(), logging.INFO))
        console_handler.setFormatter(json_formatter if settings.logging.console_json else console_formatter)
        root_logger.addHandler(console_handler)

    logging.captureWarnings(True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logging.getLogger("Jarvis.Logging").info(
        "Structured logging configured.",
        extra={
            "event": "logging.configured",
            "environment": settings.environment,
            "log_directory": str(log_directory),
            "console_enabled": settings.logging.console_enabled,
            "console_json": settings.logging.console_json,
        },
    )


def attach_event_bus_logging(event_bus) -> None:
    logger = logging.getLogger("Jarvis.EventStream")
    event_names = (
        "action.completed",
        "action.failed",
        "action.started",
        "execution.cancel_requested",
        "execution.feedback",
        "execution.finalized",
        "execution.step_done",
        "execution.step_failed",
        "execution.step_skipped",
        "execution.step_start",
        "permission.blocked",
        "permission.confirm",
        "safety.emergency_stop",
        "safety.guard_blocked",
        "safety.status",
        "safety.timeout",
        "safety.validation",
        "security.response_sent",
        "security.user_input",
    )

    def _build_handler(event_name: str):
        def _handler(*args: Any, **kwargs: Any) -> None:
            payload: Any
            if len(args) == 1 and not kwargs:
                payload = args[0]
            elif not args:
                payload = kwargs
            else:
                payload = {"args": list(args), "kwargs": kwargs}
            logger.info(
                event_name,
                extra={
                    "event": event_name,
                    "payload": safe_log_value(payload),
                },
            )

        return _handler

    for event_name in event_names:
        event_bus.subscribe(event_name, _build_handler(event_name))
