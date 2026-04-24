from __future__ import annotations

import argparse
import asyncio
import logging

from jarvis.application.orchestrator import build_application
from jarvis.config.settings import load_settings
from jarvis.observability import configure_logging
from jarvis.runtime.controller import RuntimeController, RuntimeOptions
from jarvis.runtime.utils import configure_espeak


def _parse_options() -> RuntimeOptions:
    parser = argparse.ArgumentParser(description="Jarvis AI Companion")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--voice", action="store_true", help="Enable companion voice mode")
    parser.add_argument("--companion", action="store_true", help="Enable companion voice mode (alias for --voice)")
    parser.add_argument("--gateway", action="store_true", help="Start WebSocket voice gateway")
    parser.add_argument("--gateway-port", type=int, default=8766, help="Voice gateway port (default: 8766)")
    parser.add_argument("--test-mode", action="store_true", help="Run a demo command")
    args = parser.parse_args()
    return RuntimeOptions(
        debug=args.debug,
        voice=args.voice or args.companion,
        test_mode=args.test_mode,
        companion_mode=args.voice or args.companion,
        gateway_mode=args.gateway,
        gateway_port=args.gateway_port,
    )


async def main() -> None:
    options = _parse_options()
    settings = load_settings()
    configure_espeak()
    if options.debug and not settings.debug:
        settings.debug = True
        settings.logging.level = "DEBUG"
    configure_logging(settings)
    logger = logging.getLogger("Jarvis.Main")
    logger.info(
        "Starting Jarvis initialization.",
        extra={
            "event": "startup",
            "environment": settings.environment,
            "debug": settings.debug,
        },
    )
    try:
        application = build_application(
            memory_db_path=settings.memory_db_path,
            settings=settings,
        )
        controller = RuntimeController(
            application=application,
            logger=logger,
            settings=settings,
        )
        await controller.run(options)
    except Exception:
        logger.exception("Jarvis failed during startup or runtime.")
        raise


if __name__ == "__main__":
    asyncio.run(main())
