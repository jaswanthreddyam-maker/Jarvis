from __future__ import annotations

import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from assistant.app import build_assistant
from assistant.workflow_memory import WorkflowMemory


class WorkflowMemoryTests(unittest.TestCase):
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
    def _youtube_steps(query: str) -> list[dict[str, object]]:
        return [
            {"intent": "open_url", "target": "youtube", "params": {"url": "https://www.youtube.com"}},
            {"intent": "search_youtube", "target": query, "params": {"query": query}},
        ]

    @staticmethod
    def _disable_simulation(assistant) -> None:
        action_engine = assistant._orchestrator._action_engine
        action_engine._context.settings.safe_mode = False
        action_engine._context.settings.simulate_actions = False

    def test_normalizes_youtube_variants_for_match(self) -> None:
        storage_path = Path("tests/.tmp/workflow_variants.json").resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.unlink(missing_ok=True)
        memory = WorkflowMemory(storage_path)

        memory.remember_successful_workflow("open youtube and search free fire edits", self._youtube_steps("free fire edits"))

        on_youtube_match = memory.find_similar_workflow("search lofi mix on youtube")
        lo_variant_match = memory.find_similar_workflow("youtube lo phonk edits search")

        self.assertIsNotNone(on_youtube_match)
        self.assertIsNotNone(lo_variant_match)
        self.assertEqual(on_youtube_match["steps"][1]["params"]["query"], "lofi mix")
        self.assertEqual(lo_variant_match["steps"][1]["params"]["query"], "phonk edits")
        self.assertTrue(memory._patterns[0]["intent_nuance"]["open_explicit"])

        storage_path.unlink(missing_ok=True)

    def test_validates_slot_substitution_for_dynamic_placeholders(self) -> None:
        storage_path = Path("tests/.tmp/workflow_slots.json").resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.unlink(missing_ok=True)
        memory = WorkflowMemory(storage_path)

        memory.remember_successful_workflow("open youtube and search beats", self._youtube_steps("beats"))
        match = memory.find_similar_workflow("search ambient focus on youtube")

        self.assertIsNotNone(match)
        self.assertEqual(match["slots"]["query"], "ambient focus")
        self.assertEqual(match["steps"][1]["target"], "ambient focus")
        self.assertEqual(match["steps"][1]["params"]["query"], "ambient focus")
        self.assertEqual(match["slot_semantics"]["query"]["query_type"], "keyword_phrase")
        self.assertFalse(any("<query>" in str(step) for step in match["steps"]))

        storage_path.unlink(missing_ok=True)

    def test_usage_tracking_update_is_reported_with_match(self) -> None:
        storage_path = Path("tests/.tmp/workflow_tracking.json").resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.unlink(missing_ok=True)
        memory = WorkflowMemory(storage_path)

        memory.remember_successful_workflow("open youtube and search beats", self._youtube_steps("beats"))
        match = memory.find_similar_workflow("search rain sounds on youtube")

        self.assertIsNotNone(match)
        self.assertTrue(match["usage_tracking_updated"])
        self.assertGreaterEqual(memory._patterns[0]["use_count"], 1)

        storage_path.unlink(missing_ok=True)

    def test_referential_query_does_not_reuse_without_antecedent(self) -> None:
        storage_path = Path("tests/.tmp/workflow_reference.json").resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.unlink(missing_ok=True)
        memory = WorkflowMemory(storage_path)

        memory.remember_successful_workflow("open youtube and search beats", self._youtube_steps("beats"))
        match = memory.find_similar_workflow("search it on youtube")

        self.assertIsNone(match)

        storage_path.unlink(missing_ok=True)

    def test_prunes_with_weighted_retention_score(self) -> None:
        storage_path = Path("tests/.tmp/workflow_memory.json").resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.unlink(missing_ok=True)
        memory = WorkflowMemory(storage_path, max_patterns=2)
        now = time.time()

        memory._patterns = [
            {
                "pattern": "open youtube and search <query>",
                "context": {"platforms": ["youtube"]},
                "steps": [],
                "use_count": 5,
                "successful_runs": 5,
                "created_at": now - 500,
                "last_used_at": now - 60,
            },
            {
                "pattern": "open spotify and search <query>",
                "context": {"platforms": ["spotify"]},
                "steps": [],
                "use_count": 1,
                "successful_runs": 1,
                "created_at": now - 100,
                "last_used_at": now - 10,
            },
            {
                "pattern": "open github and search <query>",
                "context": {"platforms": ["github"]},
                "steps": [],
                "use_count": 1,
                "successful_runs": 1,
                "created_at": now - 10_000,
                "last_used_at": now - 10_000,
            },
        ]

        memory._prune_patterns_locked()

        patterns = {pattern["pattern"] for pattern in memory._patterns}
        self.assertEqual(len(patterns), 2)
        self.assertIn("open youtube and search <query>", patterns)
        self.assertIn("open spotify and search <query>", patterns)
        self.assertNotIn("open github and search <query>", patterns)

        storage_path.unlink(missing_ok=True)

    def test_failed_runs_reduce_confidence_and_block_reuse(self) -> None:
        storage_path = Path("tests/.tmp/workflow_failures.json").resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.unlink(missing_ok=True)
        memory = WorkflowMemory(storage_path)

        memory.remember_successful_workflow("open youtube and search beats", self._youtube_steps("beats"))
        for _ in range(4):
            memory.remember_failed_workflow("open youtube and search beats", self._youtube_steps("beats"))

        self.assertLess(memory._patterns[0]["confidence"], 0.6)
        self.assertIsNone(memory.find_similar_workflow("search synthwave on youtube"))

        storage_path.unlink(missing_ok=True)

    def test_negative_learning_penalizes_unstable_workflows(self) -> None:
        storage_path = Path("tests/.tmp/workflow_negative_learning.json").resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.unlink(missing_ok=True)
        memory = WorkflowMemory(storage_path)

        memory.remember_successful_workflow("open youtube and search beats", self._youtube_steps("beats"))
        confidence_before = memory._patterns[0]["confidence"]
        memory.remember_failed_workflow("open youtube and search beats", self._youtube_steps("beats"))
        confidence_after = memory._patterns[0]["confidence"]

        self.assertLess(confidence_after, confidence_before)
        self.assertEqual(memory._patterns[0]["failed_runs"], 1)

        storage_path.unlink(missing_ok=True)

    def test_conflicting_platform_scenarios_do_not_reuse_incorrectly(self) -> None:
        storage_path = Path("tests/.tmp/workflow_conflict.json").resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.unlink(missing_ok=True)
        memory = WorkflowMemory(storage_path)

        memory.remember_successful_workflow("open youtube and search beats", self._youtube_steps("beats"))

        self.assertIsNone(memory.find_similar_workflow("search beats on spotify"))
        self.assertIsNone(memory.find_similar_workflow("search beats on youtube and spotify"))

        storage_path.unlink(missing_ok=True)

    def test_reused_workflow_executes_from_memory(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "workflow_execution.db"
        workflow_path = db_path.with_suffix(".workflows.json")
        db_path.unlink(missing_ok=True)
        workflow_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        self._disable_simulation(assistant)

        assistant.handle_text("open youtube and search free fire edits")
        response, snapshot = assistant.handle_text("search chill beats on youtube")

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual([step["action"] for step in snapshot["steps"]], ["open_url", "search_youtube"])
        self.assertEqual(snapshot["steps"][1]["params"]["query"], "chill beats")
        self.assertTrue(snapshot["steps"][1]["result"]["verified"])
        self.assertIn("plan_source: memory", response.lower())

        db_path.unlink(missing_ok=True)
        workflow_path.unlink(missing_ok=True)

    def test_implicit_variant_reuse_executes_correctly(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "workflow_execution_variant.db"
        workflow_path = db_path.with_suffix(".workflows.json")
        db_path.unlink(missing_ok=True)
        workflow_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)
        self._disable_simulation(assistant)

        assistant.handle_text("open youtube and search free fire edits")
        response, snapshot = assistant.handle_text("youtube lo chill beats search")

        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual([step["action"] for step in snapshot["steps"]], ["open_url", "search_youtube"])
        self.assertEqual(snapshot["steps"][1]["params"]["query"], "chill beats")
        self.assertIn("plan_source: memory", response.lower())

        db_path.unlink(missing_ok=True)
        workflow_path.unlink(missing_ok=True)

    def test_simulated_runs_do_not_seed_reusable_workflows(self) -> None:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_temp_dir / "workflow_simulated_guard.db"
        workflow_path = db_path.with_suffix(".workflows.json")
        db_path.unlink(missing_ok=True)
        workflow_path.unlink(missing_ok=True)
        assistant = build_assistant(memory_db_path=db_path)

        response, snapshot = assistant.handle_text("open youtube and search free fire edits")
        strategy_records = assistant._orchestrator._memory.recall(
            query="",
            namespace="planner_strategies",
            limit=20,
        )

        self.assertEqual(snapshot["status"], "completed")
        self.assertTrue(snapshot["steps"][0]["result"].get("simulated"))
        self.assertEqual(strategy_records, [])
        if workflow_path.exists():
            payload = workflow_path.read_text(encoding="utf-8")
            self.assertNotIn("open youtube and search <query>", payload)

        replay_response, replay_snapshot = assistant.handle_text("search chill beats on youtube")

        self.assertEqual(replay_snapshot["status"], "completed")
        self.assertNotIn("plan_source: memory", replay_response.lower())

        db_path.unlink(missing_ok=True)
        workflow_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
