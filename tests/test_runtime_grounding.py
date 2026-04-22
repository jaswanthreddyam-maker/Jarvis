from __future__ import annotations

import unittest
from unittest.mock import patch

from assistant.contracts import StepDefinition, StepState, TaskPlan, TaskState
from assistant.execution_validator import ExecutionValidator
from assistant.runtime_observer import RuntimeObserver


class _MemoryStub:
    def recall(self, *args, **kwargs):
        del args, kwargs
        return []


class _ObserverStub:
    def capture(self, **kwargs):
        del kwargs
        return {
            "observed": True,
            "observation_mode": "runtime",
            "window_title": "YouTube - Google Chrome",
            "active_app": "youtube",
            "browser_app": "chrome",
            "url": "youtube.com/results?search_query=lofi",
            "playback_state": "",
            "confidence": 0.9,
            "observed_at": "2026-04-20T00:00:00+00:00",
        }


class _MismatchedObserverStub:
    def capture(self, **kwargs):
        del kwargs
        return {
            "observed": True,
            "observation_mode": "runtime",
            "window_title": "Jarvis - execution_validator.py",
            "active_app": "",
            "browser_app": "",
            "url": "",
            "playback_state": "",
            "confidence": 0.35,
            "observed_at": "2026-04-20T00:00:00+00:00",
        }


class RuntimeGroundingTests(unittest.TestCase):
    def test_runtime_observer_infers_browser_video_state_from_window_title(self) -> None:
        observer = RuntimeObserver()
        with patch("assistant.runtime_observer.window_control.get_active_window_title", return_value="Lofi Mix - YouTube - Google Chrome"):
            with patch("assistant.runtime_observer.app_control.is_app_running", return_value=False):
                observation = observer.capture(
                    action="play_youtube",
                    result_data={},
                    expected_state={"state": "video_playing", "active_app": "youtube"},
                    simulated=False,
                )

        self.assertTrue(observation["observed"])
        self.assertEqual(observation["active_app"], "youtube")
        self.assertEqual(observation["browser_app"], "chrome")
        self.assertIn("youtube.com/watch", observation["url"])
        self.assertEqual(observation["playback_state"], "playing")

    def test_runtime_observer_detects_comet_browser_from_window_title(self) -> None:
        observer = RuntimeObserver()
        with patch("assistant.runtime_observer.window_control.get_active_window_title", return_value="YouTube - Comet"):
            with patch("assistant.runtime_observer.app_control.is_app_running", return_value=False):
                observation = observer.capture(
                    action="open_url",
                    result_data={},
                    expected_state={"active_app": "youtube"},
                    simulated=False,
                )

        self.assertTrue(observation["observed"])
        self.assertEqual(observation["active_app"], "youtube")
        self.assertEqual(observation["browser_app"], "comet")
        self.assertIn("youtube.com", observation["url"])

    def test_runtime_observer_does_not_reuse_stale_browser_state_from_result_data(self) -> None:
        observer = RuntimeObserver()
        with patch(
            "assistant.runtime_observer.window_control.get_active_window_title",
            return_value="Jarvis - execution_validator.py",
        ):
            with patch("assistant.runtime_observer.app_control.is_app_running", return_value=True):
                observation = observer.capture(
                    action="open_url",
                    result_data={
                        "url": "https://www.youtube.com",
                        "browser_app": "comet",
                        "active_app": "youtube",
                    },
                    expected_state={"active_app": "youtube"},
                    simulated=False,
                )

        self.assertTrue(observation["observed"])
        self.assertEqual(observation["active_app"], "")
        self.assertEqual(observation["browser_app"], "")
        self.assertEqual(observation["url"], "")

    def test_goal_progress_uses_runtime_observation_when_result_lacks_url(self) -> None:
        validator = ExecutionValidator(_MemoryStub(), observer=_ObserverStub())
        plan = TaskPlan(
            intent="search_youtube",
            steps=[
                StepDefinition(
                    action="search_youtube",
                    step_id=1,
                    target="lofi",
                    params={"query": "lofi"},
                )
            ],
            goal_state={
                "action": "search_youtube",
                "state": "results_loaded",
                "active_app": "youtube",
                "platform": "youtube",
                "query": "lofi",
                "url_contains": "youtube.com/results",
                "requires_verified_final_step": True,
            },
        )
        task = TaskState(
            task_id="task-1",
            user_input="search youtube for lofi",
            intent="search_youtube",
            steps=[
                StepState(
                    step_id="task-1-step-1",
                    plan_step_id=1,
                    action="search_youtube",
                    target="lofi",
                    params={"query": "lofi"},
                    description="Search YouTube for lofi.",
                    verification="confirm the YouTube search URL was prepared",
                    status="completed",
                    result={"verified": True},
                )
            ],
        )

        progress = validator.goal_progress(
            plan,
            task=task,
            workflow_snapshot={"active_app": "", "workflow": []},
        )

        self.assertTrue(progress["achieved"])
        self.assertEqual(progress["observed"]["active_app"], "youtube")
        self.assertIn("youtube.com/results", progress["observed"]["url"])
        self.assertEqual(progress["observed"]["observation_mode"], "runtime")

    def test_goal_progress_prefers_runtime_observation_over_prepared_result(self) -> None:
        validator = ExecutionValidator(_MemoryStub(), observer=_MismatchedObserverStub())
        plan = TaskPlan(
            intent="open_url",
            steps=[
                StepDefinition(
                    action="open_url",
                    step_id=1,
                    target="youtube",
                    params={"url": "https://www.youtube.com"},
                )
            ],
            goal_state={
                "action": "open_url",
                "state": "page_loaded",
                "active_app": "youtube",
                "platform": "youtube",
                "url_contains": "https://www.youtube.com",
                "requires_verified_final_step": True,
            },
        )
        task = TaskState(
            task_id="task-2",
            user_input="open youtube",
            intent="open_url",
            steps=[
                StepState(
                    step_id="task-2-step-1",
                    plan_step_id=1,
                    action="open_url",
                    target="youtube",
                    params={"url": "https://www.youtube.com"},
                    description="Open youtube in the browser.",
                    verification="confirm youtube opened",
                    status="completed",
                    result={
                        "verified": True,
                        "url": "https://www.youtube.com",
                        "active_app": "youtube",
                    },
                )
            ],
        )

        progress = validator.goal_progress(
            plan,
            task=task,
            workflow_snapshot={"active_app": "youtube", "workflow": []},
        )

        self.assertFalse(progress["achieved"])
        self.assertEqual(progress["observed"]["active_app"], "")
        self.assertEqual(progress["observed"]["url"], "")
        self.assertEqual(progress["observed"]["window_title"], "Jarvis - execution_validator.py")


if __name__ == "__main__":
    unittest.main()
