from __future__ import annotations

from pathlib import Path
import unittest

from assistant.memory.memory import MemoryManager


class MemoryManagerTests(unittest.TestCase):
    def test_remember_and_recall(self) -> None:
        schema_path = Path("assistant/memory/db_schema.sql").resolve()
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "memory_test.db"
        db_path.unlink(missing_ok=True)
        manager = MemoryManager(db_path, schema_path)
        manager.remember(namespace="system", content="User likes dark mode", category="preference")

        matches = manager.recall(query="dark mode", namespace="system")
        db_path.unlink(missing_ok=True)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["content"], "User likes dark mode")


if __name__ == "__main__":
    unittest.main()
