from __future__ import annotations

import unittest
from pathlib import Path

from assistant.runtime_config import get_audio_config, merge_audio_config


class RuntimeConfigTests(unittest.TestCase):
    def test_merge_audio_config_applies_stable_defaults(self) -> None:
        merged = merge_audio_config({"sample_rate": 8000})

        self.assertEqual(merged["sample_rate"], 8000)
        self.assertEqual(merged["whisper_model"], "tiny.en")
        self.assertFalse(merged["background_wake_enabled"])
        self.assertGreater(merged["background_wake_interval_seconds"], 1.0)

    def test_get_audio_config_reads_project_config(self) -> None:
        config = get_audio_config(Path("assistant/config/config.yaml"))

        self.assertEqual(config["whisper_model"], "tiny.en")
        self.assertFalse(config["background_wake_enabled"])


if __name__ == "__main__":
    unittest.main()
