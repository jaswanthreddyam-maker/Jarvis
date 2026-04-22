from __future__ import annotations

import time
import unittest

from assistant.session_memory import SessionMemory


class ContextManagerTests(unittest.TestCase):
    def test_context_expires_after_ttl(self) -> None:
        memory = SessionMemory(ttl_seconds=0.01)
        memory.remember_action(
            user_input="open chrome",
            action="open_app",
            target="chrome",
            params={"app_name": "chrome"},
        )

        time.sleep(0.02)
        snapshot = memory.snapshot()

        self.assertEqual(snapshot.last_command, "")
        self.assertEqual(snapshot.recent_contexts, ())


if __name__ == "__main__":
    unittest.main()
