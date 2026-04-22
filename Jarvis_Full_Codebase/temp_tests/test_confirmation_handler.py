from __future__ import annotations

import time
import unittest

from assistant.confirmation_handler import ConfirmationHandler
from assistant.contracts import StepDefinition, TaskPlan


class ConfirmationHandlerTests(unittest.TestCase):
    def test_yes_executes_pending_plan(self) -> None:
        handler = ConfirmationHandler(timeout_seconds=5.0)
        plan = TaskPlan(intent="delete_file", steps=[StepDefinition(action="delete_file", step_id=1, target="notes.txt")])
        handler.queue("delete file notes.txt", plan, prompt="Are you sure?")

        resolution = handler.resolve("yes")

        self.assertIsNotNone(resolution)
        self.assertTrue(resolution.approved)
        self.assertEqual(resolution.plan.steps[0].action, "delete_file")

    def test_timeout_cancels_pending_plan(self) -> None:
        handler = ConfirmationHandler(timeout_seconds=0.01)
        plan = TaskPlan(intent="delete_file", steps=[StepDefinition(action="delete_file", step_id=1, target="notes.txt")])
        handler.queue("delete file notes.txt", plan, prompt="Are you sure?")
        time.sleep(0.02)

        resolution = handler.resolve("yes")

        self.assertIsNotNone(resolution)
        self.assertFalse(resolution.approved)
        self.assertIn("expired", resolution.response.lower())


if __name__ == "__main__":
    unittest.main()
