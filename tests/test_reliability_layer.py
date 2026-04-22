from __future__ import annotations

import unittest

from assistant.confirmation_handler import ConfirmationHandler
from assistant.contracts import RiskLevel, StepDefinition, TaskPlan, TaskState
from assistant.execution_finalizer import ExecutionFinalizer
from assistant.intent_session_tracker import IntentSessionTracker
from assistant.scope_validator import ActionScopeValidator


class ScopeValidatorTests(unittest.TestCase):
    def test_bulk_delete_requires_double_confirmation(self) -> None:
        validator = ActionScopeValidator()

        assessment = validator.assess("delete_file", "all files")

        self.assertTrue(assessment.valid)
        self.assertEqual(assessment.scope, "bulk")
        self.assertEqual(assessment.risk_level, RiskLevel.CRITICAL)
        self.assertEqual(assessment.confirmation_level, 2)
        self.assertEqual(assessment.safety_level, "CONFIRMATION REQUIRED")


class IntentTrackerTests(unittest.TestCase):
    def test_follow_up_with_drift_requests_clarification(self) -> None:
        tracker = IntentSessionTracker()
        tracker.remember(
            "delete notes.txt",
            TaskPlan(
                intent="delete_file",
                goal="delete notes.txt",
                steps=[StepDefinition(action="delete_file", step_id=1, target="notes.txt")],
            ),
        )

        assessment = tracker.assess(
            "open it again",
            TaskPlan(
                intent="open_app",
                goal="open it again",
                steps=[StepDefinition(action="open_app", step_id=1, target="chrome")],
            ),
        )

        self.assertIsNotNone(assessment.clarification_question)
        self.assertTrue(assessment.intent_changed)


class ExecutionFinalizerTests(unittest.TestCase):
    def test_interrupted_execution_reports_completed_and_skipped_steps(self) -> None:
        task = TaskState(
            task_id="task-1",
            user_input="open chrome and search youtube",
            intent="multi_step_command",
        )
        task.status = "interrupted"
        task.steps = [
            type("Step", (), {"description": "Open the chrome application.", "action": "open_app", "status": "completed"})(),
            type("Step", (), {"description": "Search the web for youtube.", "action": "search_web", "status": "skipped"})(),
        ]

        finalizer = ExecutionFinalizer()
        result = finalizer.finalize(task, "Stopped.")

        self.assertIn("Cancelled safely", result.response)
        self.assertIn("Open the chrome application", result.response)
        self.assertIn("Search the web for youtube", result.response)


class DoubleConfirmationTests(unittest.TestCase):
    def test_high_risk_scope_requires_two_yes_responses(self) -> None:
        handler = ConfirmationHandler(timeout_seconds=5.0)
        plan = TaskPlan(
            intent="delete_file",
            steps=[
                StepDefinition(
                    action="delete_file",
                    step_id=1,
                    target="all files",
                    params={
                        "_confirmation_level": 2,
                        "_validation_reason": "This action appears to affect multiple files or a broad scope.",
                    },
                )
            ],
        )

        prompt = handler.queue("delete all files", plan)
        first = handler.resolve("yes")
        second = handler.resolve("yes")

        self.assertIn("reply yes twice", prompt.lower())
        self.assertIsNotNone(first)
        self.assertFalse(first.approved)
        self.assertIn("confirm again", first.response.lower())
        self.assertIsNotNone(second)
        self.assertTrue(second.approved)


if __name__ == "__main__":
    unittest.main()
