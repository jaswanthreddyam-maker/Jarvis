from __future__ import annotations

import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from assistant.contracts import ActionResult, StepDefinition, TaskPlan
from assistant.app import build_assistant


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_simulate = os.environ.get("JARVIS_SIMULATE_ACTIONS")
        os.environ["JARVIS_SIMULATE_ACTIONS"] = "1"
        self._webbrowser_patcher = patch("assistant.tools.web_control.webbrowser.open", return_value=True)
        self._popen_patcher = patch("assistant.tools.app_control.subprocess.Popen")
        self._web_popen_patcher = patch("assistant.tools.web_control.subprocess.Popen")
        self._window_title_patcher = patch("assistant.runtime_observer.window_control.get_active_window_title", return_value="")
        self._webbrowser_patcher.start()
        self._popen_patcher.start()
        self._web_popen_patcher.start()
        self._window_title_patcher.start()

    def tearDown(self) -> None:
        self._webbrowser_patcher.stop()
        self._popen_patcher.stop()
        self._web_popen_patcher.stop()
        self._window_title_patcher.stop()
        if self._previous_simulate is None:
            os.environ.pop("JARVIS_SIMULATE_ACTIONS", None)
        else:
            os.environ["JARVIS_SIMULATE_ACTIONS"] = self._previous_simulate

    @staticmethod
    def _disable_simulation(assistant) -> None:
        action_engine = assistant._orchestrator._action_engine
        action_engine._context.settings.safe_mode = False
        action_engine._context.settings.simulate_actions = False

    def test_open_youtube_executes_without_confirmation(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "open_youtube.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        response, snapshot = assistant.handle_text("open youtube")
        db_path.unlink(missing_ok=True)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["steps"][0]["action"], "open_url")
        self.assertTrue(snapshot["steps"][0]["result"]["verified"])
        self.assertTrue(response.lower().startswith("done."))
        self.assertIn("youtube", response.lower())

    def test_memory_persists_across_instances(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "persistence.db"
        db_path.unlink(missing_ok=True)
        assistant_one = build_assistant(memory_db_path=db_path)
        self.assertEqual(
            assistant_one.handle_text("remember that my favorite editor is vscode")[1]["status"],
            "completed",
        )

        assistant_two = build_assistant(memory_db_path=db_path)
        response, snapshot = assistant_two.handle_text("what do you remember about editor")
        db_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "completed")
        self.assertIn("favorite editor", response.lower())

    def test_due_reminder_emits_notification(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "reminder.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        assistant.handle_text("remind me to stretch in 0 seconds")
        notifications = assistant.poll_notifications()
        db_path.unlink(missing_ok=True)

        self.assertTrue(any("stretch" in notification.lower() for notification in notifications))

    def test_successful_plan_is_reused_from_memory(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "reuse_plan.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        self._disable_simulation(assistant)
        assistant.handle_text("open youtube")

        preview, snapshot = assistant.handle_text("open youtube")
        db_path.unlink(missing_ok=True)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["status"], "completed")
        self.assertIn("plan_source: memory", preview.lower())

    def test_multi_step_command_executes_in_sequence(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "multi_step.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        response, snapshot = assistant.handle_text("open chrome and search youtube")
        db_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual([step["action"] for step in snapshot["steps"]], ["open_app", "search_web"])
        self.assertIn("done.", response.lower())

    def test_repeat_last_action_uses_session_memory(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "repeat_last.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        assistant.handle_text("open youtube")
        response, snapshot = assistant.handle_text("open it again")
        db_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["steps"][0]["action"], "open_url")
        self.assertIn("youtube", response.lower())

    def test_context_continuity_uses_last_browser_for_search(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "continuity_browser.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        assistant.handle_text("open chrome")
        response, snapshot = assistant.handle_text("search youtube")
        db_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["steps"][0]["action"], "search_web")
        self.assertEqual(snapshot["steps"][0]["params"]["browser_app"], "chrome")
        self.assertIn("chrome", response.lower())

    def test_plan_marks_browser_search_dependency(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "dependency_plan.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        plan = assistant._orchestrator._planner.plan("open chrome and search youtube")
        db_path.unlink(missing_ok=True)

        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[1].depends_on, (1,))
        self.assertEqual(plan.steps[1].param_bindings["browser_app"], "step_1.data.app_name")

    def test_open_youtube_and_search_uses_connected_youtube_workflow(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "youtube_chain.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)

        response, snapshot = assistant.handle_text("open youtube and search free fire edits")
        db_path.unlink(missing_ok=True)
        db_path.with_suffix(".workflows.json").unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual([step["action"] for step in snapshot["steps"]], ["open_url", "search_youtube"])
        self.assertEqual(snapshot["steps"][1]["depends_on"], (1,))
        self.assertEqual(snapshot["steps"][1]["params"]["query"], "free fire edits")
        self.assertIn("youtube", response.lower())

    def test_successful_youtube_workflow_is_reused_with_new_query(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "youtube_reuse.db"
        db_path.unlink(missing_ok=True)
        workflow_path = db_path.with_suffix(".workflows.json")
        workflow_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        self._disable_simulation(assistant)

        assistant.handle_text("open youtube and search free fire edits")
        response, snapshot = assistant.handle_text("open youtube and search lofi mix")

        db_path.unlink(missing_ok=True)
        workflow_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual([step["action"] for step in snapshot["steps"]], ["open_url", "search_youtube"])
        self.assertEqual(snapshot["steps"][1]["params"]["query"], "lofi mix")
        self.assertIn("plan_source: memory", response.lower())

    def test_restricted_action_requests_confirmation(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "confirm_delete.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        action_engine = assistant._orchestrator._action_engine
        self._disable_simulation(assistant)

        target = Path("tests/.tmp/delete_me.txt").resolve()
        target.write_text("delete me", encoding="utf-8")
        self.addCleanup(lambda: target.unlink(missing_ok=True))

        prompt, prompt_snapshot = assistant.handle_text("delete file tests/.tmp/delete_me.txt")
        self.assertEqual(prompt_snapshot["status"], "completed")
        self.assertEqual(prompt_snapshot["steps"], [])
        self.assertIn("are you sure", prompt.lower())

        response, snapshot = assistant.handle_text("yes")
        db_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "completed")
        self.assertFalse(target.exists())
        self.assertTrue(response.lower().startswith("done."))

    def test_ambiguous_open_request_asks_for_clarification(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "clarify_open.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        response, snapshot = assistant.handle_text("open something")
        db_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["steps"], [])
        self.assertEqual(response, "What do you want me to open?")

    def test_partial_failure_can_offer_default_browser_recovery(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "recovery_browser.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        action_engine = assistant._orchestrator._action_engine
        self._disable_simulation(assistant)

        with patch("assistant.tools.app_control.subprocess.Popen", side_effect=OSError("missing")):
            prompt, snapshot = assistant.handle_text("open chrome and search youtube")

        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("default browser", prompt.lower())

        response, retry_snapshot = assistant.handle_text("yes")
        db_path.unlink(missing_ok=True)

        self.assertEqual(retry_snapshot["status"], "completed")
        self.assertEqual(retry_snapshot["steps"][0]["action"], "search_web")
        self.assertNotIn("browser_app", retry_snapshot["steps"][0]["params"])
        self.assertTrue(response.lower().startswith("done."))

    def test_safe_mode_block_is_recorded_as_permission_block(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "permission_block.db"
        db_path.unlink(missing_ok=True)
        target = Path("tests/.tmp/blocked_delete.txt").resolve()
        target.write_text("blocked", encoding="utf-8")
        self.addCleanup(lambda: target.unlink(missing_ok=True))

        assistant = build_assistant(memory_db_path=db_path)
        action_engine = assistant._orchestrator._action_engine
        action_engine._context.settings.safe_mode = True
        action_engine._context.settings.simulate_actions = False

        assistant.handle_text("delete file tests/.tmp/blocked_delete.txt")
        response, snapshot = assistant.handle_text("yes")
        records = assistant._orchestrator._memory.recall(
            query="",
            namespace="planner_strategies",
            limit=10,
        )

        db_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("blocked by policy", response.lower())
        self.assertTrue(any('"failure_reason": "permission_block"' in record["content"] for record in records))

    def test_interrupt_stops_multi_step_plan_cleanly(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "interrupt_plan.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        orchestrator = assistant._orchestrator
        action_engine = orchestrator._action_engine
        registry = action_engine._registry

        def wait_action(params, context):
            deadline = time.time() + float(params.get("seconds", 1.0))
            while time.time() < deadline:
                token = getattr(context, "cancellation_token", None)
                if token is not None:
                    token.raise_if_cancelled("Cancelled the waiting step.")
                time.sleep(0.02)
            return ActionResult(success=True, message="Waited.")

        registry.register("wait_action", wait_action)

        class InterruptPlanner:
            def plan(self, user_input: str) -> TaskPlan:
                del user_input
                return TaskPlan(
                    intent="interrupt_test",
                    steps=[
                        StepDefinition(
                            action="wait_action",
                            step_id=1,
                            target="delay",
                            params={"seconds": 0.5},
                            description="Wait for cancellation.",
                        ),
                        StepDefinition(
                            action="get_time",
                            step_id=2,
                            description="Get the local system time.",
                        ),
                    ],
                )

            def start_execution(self, plan: TaskPlan) -> None:
                del plan

            def finish_execution(self) -> None:
                pass

            def verify_step(self, step, result):
                return result.success, result.message

        original_planner = orchestrator._planner
        orchestrator._planner = InterruptPlanner()
        self.addCleanup(setattr, orchestrator, "_planner", original_planner)

        result_holder: dict[str, object] = {}

        def _run() -> None:
            response, snapshot = assistant.handle_text("interrupt test")
            result_holder["response"] = response
            result_holder["snapshot"] = snapshot

        assistant.begin_execution("interrupt-test")
        worker = threading.Thread(target=_run)
        worker.start()
        time.sleep(0.1)
        assistant.cancel_active("interrupt-test")
        worker.join(timeout=5.0)
        assistant.finish_execution("interrupt-test")
        db_path.unlink(missing_ok=True)

        snapshot = result_holder["snapshot"]
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["status"], "interrupted")
        self.assertEqual(snapshot["steps"][1]["status"], "skipped")
        self.assertIn("cancel", str(result_holder["response"]).lower())

    def test_recent_browser_conflict_can_ask_for_clarification(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "browser_conflict.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        assistant.handle_text("open chrome")
        assistant.handle_text("open firefox")

        response, snapshot = assistant.handle_text("search youtube")
        db_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["steps"], [])
        self.assertIn("chrome", response.lower())
        self.assertIn("firefox", response.lower())

    def test_play_it_uses_latest_youtube_query_but_requires_playback_goal(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "play_goal_gate.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        self._disable_simulation(assistant)

        assistant.handle_text("open youtube")
        assistant.handle_text("search AI videos")
        assistant.handle_text("search python tutorials")
        response, snapshot = assistant.handle_text("play it")
        recovery_response, recovery_snapshot = assistant.handle_text("yes")
        strategy_records = assistant._orchestrator._memory.recall(
            query="",
            namespace="planner_strategies",
            limit=10,
        )

        db_path.unlink(missing_ok=True)
        db_path.with_suffix(".workflows.json").unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["steps"][-1]["action"], "play_youtube")
        self.assertEqual(snapshot["steps"][-1]["params"]["query"], "python tutorials")
        self.assertTrue(snapshot["steps"][-1]["result"].get("fallback_used"))
        self.assertIn("video", response.lower())
        self.assertIn("reply yes", response.lower())
        self.assertEqual(recovery_snapshot["status"], "completed")
        self.assertEqual(
            [step["action"] for step in recovery_snapshot["steps"]],
            ["open_url", "search_youtube"],
        )
        self.assertEqual(recovery_snapshot["steps"][-1]["params"]["query"], "python tutorials")
        self.assertIn("done", recovery_response.lower())
        self.assertTrue(any("youtube_results_recovery" in record["content"] for record in strategy_records))
        self.assertTrue(any('"success": true' in record["content"] for record in strategy_records))
        self.assertTrue(any('"success": false' in record["content"] for record in strategy_records))

    def test_multiple_platforms_in_single_command_require_choice(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "platform_conflict.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)

        response, snapshot = assistant.handle_text("search youtube and spotify")
        db_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["steps"], [])
        self.assertIn("multiple platforms", response.lower())
        self.assertIn("youtube", response.lower())
        self.assertIn("spotify", response.lower())

    def test_goal_completion_gate_marks_task_failed_when_goal_not_confirmed(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "goal_gate.db"
        db_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        orchestrator = assistant._orchestrator

        class GoalGatePlanner:
            def plan(self, user_input: str) -> TaskPlan:
                del user_input
                return TaskPlan(
                    intent="goal_gate_test",
                    steps=[
                        StepDefinition(
                            action="get_time",
                            step_id=1,
                            description="Get the local system time.",
                        )
                    ],
                )

            def start_execution(self, plan: TaskPlan) -> None:
                del plan

            def finish_execution(self) -> None:
                pass

            def verify_step(self, step, result):
                return result.success, result.message

            def check_goal_completion(self, plan, *, task, workflow_snapshot):
                del plan, task, workflow_snapshot
                return False, "Goal state was not achieved."

        original_planner = orchestrator._planner
        orchestrator._planner = GoalGatePlanner()
        self.addCleanup(setattr, orchestrator, "_planner", original_planner)

        response, snapshot = assistant.handle_text("goal gate test")
        db_path.unlink(missing_ok=True)

        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("goal state", response.lower())


if __name__ == "__main__":
    unittest.main()
