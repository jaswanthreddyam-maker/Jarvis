from __future__ import annotations

from pathlib import Path
import unittest

from jarvis.core.context import ExecutionPlan, ExecutionStep
from jarvis.core.memory import LongTermMemory, MemoryManager, SemanticMemory, ShortTermMemory
from tests.support_embeddings import FakeEmbeddingProvider


class MemoryManagerTests(unittest.TestCase):
    @staticmethod
    def _manager(base_name: str) -> tuple[MemoryManager, Path, Path]:
        base_temp_dir = Path("tests/.tmp").resolve()
        base_temp_dir.mkdir(parents=True, exist_ok=True)
        long_term_path = base_temp_dir / f"{base_name}.long_term.json"
        semantic_path = base_temp_dir / f"{base_name}.semantic.json"
        long_term_path.unlink(missing_ok=True)
        semantic_path.unlink(missing_ok=True)
        manager = MemoryManager(
            short_term=ShortTermMemory(),
            long_term=LongTermMemory(long_term_path),
            semantic=SemanticMemory(
                semantic_path,
                embedding_provider=FakeEmbeddingProvider(),
            ),
        )
        return manager, long_term_path, semantic_path

    def test_remember_and_recall(self) -> None:
        manager, long_term_path, semantic_path = self._manager("memory_test")
        manager.remember(namespace="system", content="User likes dark mode", category="preference")

        matches = manager.recall(query="dark mode", namespace="system")
        long_term_path.unlink(missing_ok=True)
        semantic_path.unlink(missing_ok=True)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["content"], "User likes dark mode")

    def test_short_term_memory_keeps_recent_interactions(self) -> None:
        manager, long_term_path, semantic_path = self._manager("memory_short_term")
        manager.add_interaction("open youtube", "Done. opened youtube.")
        manager.add_interaction("search lofi", "Done. searched lofi.")
        manager.add_interaction("increase volume", "Done. adjusted the volume.")

        recent = manager.recent_conversation(limit=2)
        long_term_path.unlink(missing_ok=True)
        semantic_path.unlink(missing_ok=True)

        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["user"], "search lofi")
        self.assertEqual(recent[1]["user"], "increase volume")

    def test_long_term_preferences_persist_across_instances(self) -> None:
        manager, long_term_path, semantic_path = self._manager("memory_preferences")
        manager.save_preference("preferred_browser", "chrome", source="test")

        reloaded = MemoryManager(
            short_term=ShortTermMemory(),
            long_term=LongTermMemory(long_term_path),
            semantic=SemanticMemory(
                semantic_path,
                embedding_provider=FakeEmbeddingProvider(),
            ),
        )
        preference = reloaded.get_preference("preferred_browser")
        long_term_path.unlink(missing_ok=True)
        semantic_path.unlink(missing_ok=True)

        self.assertEqual(preference, "chrome")

    def test_semantic_memory_retrieves_similar_entries(self) -> None:
        manager, long_term_path, semantic_path = self._manager("memory_semantic")
        manager.store_memory("User enjoys lofi beats during work.", metadata={"category": "preference"})

        matches = manager.retrieve_similar("play some lofi music", limit=3)
        long_term_path.unlink(missing_ok=True)
        semantic_path.unlink(missing_ok=True)

        self.assertTrue(matches)
        self.assertIn("lofi", matches[0]["text"].lower())

    def test_build_context_includes_short_long_and_semantic_memory(self) -> None:
        manager, long_term_path, semantic_path = self._manager("memory_context")
        manager.add_interaction("play music", "What kind of music?")
        manager.save_preference("favorite_music", "lofi", source="test")
        manager.remember(namespace="system", content="User studies better with background music.", category="fact")
        manager.store_memory("User prefers calm lofi playlists.", metadata={"category": "preference"})

        context = manager.build_context("play music").as_payload()
        long_term_path.unlink(missing_ok=True)
        semantic_path.unlink(missing_ok=True)

        self.assertEqual(context["short_term"][0]["user"], "play music")
        self.assertEqual(context["long_term"]["preferences"]["favorite_music"], "lofi")
        self.assertTrue(context["semantic"])

    def test_sensitive_content_is_not_persisted(self) -> None:
        manager, long_term_path, semantic_path = self._manager("memory_sensitive")

        stored = manager.remember(namespace="system", content="My password is hunter2", category="fact")
        matches = manager.recall(query="hunter2", namespace="system")
        long_term_path.unlink(missing_ok=True)
        semantic_path.unlink(missing_ok=True)

        self.assertFalse(stored)
        self.assertEqual(matches, [])

    def test_credit_card_like_numbers_are_not_persisted(self) -> None:
        manager, long_term_path, semantic_path = self._manager("memory_sensitive_card")

        stored = manager.remember(
            namespace="system",
            content="My card number is 1234 5678 1234 5678",
            category="fact",
        )
        matches = manager.recall(query="1234 5678 1234 5678", namespace="system")
        long_term_path.unlink(missing_ok=True)
        semantic_path.unlink(missing_ok=True)

        self.assertFalse(stored)
        self.assertEqual(matches, [])

    def test_preferences_can_personalize_a_plan_outside_memory_core(self) -> None:
        manager, long_term_path, semantic_path = self._manager("memory_personalization")
        manager.save_preference("preferred_browser", "chrome", source="test")
        manager.save_preference("favorite_music", "lofi", source="test")
        plan = ExecutionPlan(
            intent="multi_step_command",
            goal="play music and search docs",
            steps=[
                ExecutionStep(action="play_youtube", step_id=1, target="music", params={"query": "music"}),
                ExecutionStep(action="search_web", step_id=2, target="python docs", params={"query": "python docs"}),
            ],
        )

        if manager.get_preference("favorite_music"):
            plan.steps[0].params["query"] = f"{manager.get_preference('favorite_music')} music"
        if manager.get_preference("preferred_browser"):
            plan.steps[1].params["browser_app"] = str(manager.get_preference("preferred_browser"))

        long_term_path.unlink(missing_ok=True)
        semantic_path.unlink(missing_ok=True)
        self.assertEqual(plan.steps[0].params["query"], "lofi music")
        self.assertEqual(plan.steps[1].params["browser_app"], "chrome")


if __name__ == "__main__":
    unittest.main()
