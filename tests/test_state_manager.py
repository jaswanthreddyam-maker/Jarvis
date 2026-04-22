from __future__ import annotations

import unittest

from assistant.contracts import ActionResult, StepDefinition, TaskPlan
from assistant.state_manager import TaskStateManager


class TaskStateManagerTests(unittest.TestCase):
    def test_task_lifecycle_snapshot(self) -> None:
        manager = TaskStateManager()
        plan = TaskPlan(
            intent="get_time",
            steps=[StepDefinition(action="get_time", description="Fetch the time.")],
        )

        task = manager.create_task("what time is it", plan)
        manager.mark_task_running(task.task_id)
        manager.mark_step_running(task.task_id, task.steps[0].step_id)
        manager.mark_step_completed(
            task.task_id,
            task.steps[0].step_id,
            ActionResult(success=True, message="The local time is 10:00:00."),
        )
        manager.mark_task_completed(task.task_id)
        manager.set_final_response(task.task_id, "The local time is 10:00:00.")

        snapshot = manager.snapshot(task.task_id)

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["steps"][0]["status"], "completed")
        self.assertEqual(snapshot["steps"][0]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
