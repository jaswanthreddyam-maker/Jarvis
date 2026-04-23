from __future__ import annotations

import json
import logging
import os
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from config.settings import load_settings
from jarvis.observability.logging import configure_logging


class StructuredLoggingTests(unittest.TestCase):
    def test_json_log_file_contains_context(self) -> None:
        tmp_root = Path("tests/.tmp") / f"logs-{uuid4().hex}"
        tmp_root.mkdir(parents=True, exist_ok=True)
        previous = {key: os.environ.get(key) for key in ("JARVIS_LOG_DIR", "JARVIS_CONSOLE_LOGGING")}
        try:
            os.environ["JARVIS_LOG_DIR"] = str(tmp_root)
            os.environ["JARVIS_CONSOLE_LOGGING"] = "false"
            settings = load_settings()
            configure_logging(settings)

            logger = logging.getLogger("Jarvis.TestLogging")
            logger.info(
                "structured entry",
                extra={
                    "event": "test.structured",
                    "request_id": "req-123",
                },
            )

            for handler in logging.getLogger().handlers:
                handler.flush()

            log_path = settings.logging.app_log_path
            lines = log_path.read_text(encoding="utf-8").splitlines()
            payload = json.loads(lines[-1])
            self.assertEqual(payload["message"], "structured entry")
            self.assertEqual(payload["context"]["event"], "test.structured")
            self.assertEqual(payload["context"]["request_id"], "req-123")
        finally:
            root_logger = logging.getLogger()
            for handler in tuple(root_logger.handlers):
                root_logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
