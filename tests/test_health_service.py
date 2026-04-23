from __future__ import annotations

import os
import unittest

from config.settings import load_settings
from jarvis.monitoring.health import JarvisHealthService


class HealthServiceTests(unittest.TestCase):
    def test_snapshot_reports_provider_and_process_status(self) -> None:
        previous = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "JARVIS_HEALTH_CHECK_EXTERNAL_CONNECTIVITY")}
        try:
            os.environ["OPENAI_API_KEY"] = "test-key"
            os.environ["JARVIS_HEALTH_CHECK_EXTERNAL_CONNECTIVITY"] = "false"
            settings = load_settings()
            service = JarvisHealthService(settings)
            snapshot = service.snapshot()

            self.assertIn(snapshot["status"], {"ok", "degraded"})
            self.assertIn("process", snapshot)
            self.assertIn("providers", snapshot)
            self.assertEqual(snapshot["providers"]["openai"]["status"], "configured")
            self.assertIn(snapshot["providers"]["ollama"]["status"], {"ok", "error", "configured"})
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
