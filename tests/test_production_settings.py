from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from config.settings import load_settings


class ProductionSettingsTests(unittest.TestCase):
    def test_environment_variables_override_runtime_defaults(self) -> None:
        tmp_root = Path("tests/.tmp") / f"settings-{uuid4().hex}"
        tmp_root.mkdir(parents=True, exist_ok=True)
        memory_path = tmp_root / "memory.db"
        log_dir = tmp_root / "logs"
        previous = {key: os.environ.get(key) for key in (
            "JARVIS_ENV",
            "JARVIS_LOG_DIR",
            "JARVIS_MAX_CONCURRENT_REQUESTS",
            "JARVIS_HEALTH_PORT",
            "JARVIS_CONSOLE_LOGGING",
        )}
        try:
            os.environ["JARVIS_ENV"] = "production"
            os.environ["JARVIS_LOG_DIR"] = str(log_dir)
            os.environ["JARVIS_MAX_CONCURRENT_REQUESTS"] = "4"
            os.environ["JARVIS_HEALTH_PORT"] = "9001"
            os.environ["JARVIS_CONSOLE_LOGGING"] = "false"

            settings = load_settings(memory_db_path=memory_path)

            self.assertEqual(settings.environment, "production")
            self.assertEqual(settings.logging.directory, log_dir.resolve())
            self.assertEqual(settings.resources.max_concurrent_requests, 4)
            self.assertEqual(settings.health.port, 9001)
            self.assertFalse(settings.logging.console_enabled)
            self.assertEqual(settings.memory_db_path, memory_path.resolve())
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
