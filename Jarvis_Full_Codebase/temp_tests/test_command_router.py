from __future__ import annotations

import unittest

from assistant.command_router import CommandRouter
from assistant.execution_planner import ExecutionPlanner
from assistant.session_memory import SessionMemory


class CommandRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = CommandRouter()

    def test_routes_polite_open_request(self) -> None:
        intent = self.router.route("can you open chrome")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, "open_app")
        self.assertEqual(intent.params["app_name"], "chrome")

    def test_routes_create_file(self) -> None:
        intent = self.router.route("create a file named notes.txt")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, "create_file")
        self.assertEqual(intent.params["name"], "notes.txt")

    def test_routes_volume_change(self) -> None:
        intent = self.router.route("increase volume")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, "set_volume")
        self.assertEqual(intent.params["value"], "increase")

    def test_routes_known_folder(self) -> None:
        intent = self.router.route("show downloads folder")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, "open_explorer")
        self.assertEqual(intent.params["path"], "downloads")

    def test_routes_clipboard_read(self) -> None:
        intent = self.router.route("what is on the clipboard")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, "get_clipboard")

    def test_routes_multi_step_command(self) -> None:
        parsed = self.router.route_many("open chrome and search youtube")
        self.assertIsNone(parsed.clarification_question)
        self.assertEqual(len(parsed.intents), 2)
        self.assertEqual(parsed.intents[0].action, "open_app")
        self.assertEqual(parsed.intents[1].action, "search_web")

    def test_asks_for_clarification_when_target_is_ambiguous(self) -> None:
        parsed = self.router.route_many("open something")
        self.assertEqual(parsed.clarification_question, "What do you want me to open?")

    def test_reuses_session_memory_for_repeat_request(self) -> None:
        memory = SessionMemory()
        memory.remember_action(
            user_input="open youtube",
            action="open_url",
            target="youtube",
            params={"url": "https://www.youtube.com"},
        )
        intent = self.router.route("open it again", memory=memory.snapshot())
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, "open_url")
        self.assertEqual(intent.params["url"], "https://www.youtube.com")

    def test_suggests_known_app_for_typo(self) -> None:
        parsed = self.router.route_many("open chroem")
        self.assertEqual(parsed.clarification_question, "Did you mean Chrome?")

    def test_applies_aliases_before_routing(self) -> None:
        youtube = self.router.route("open yt")
        self.assertIsNotNone(youtube)
        self.assertEqual(youtube.action, "open_url")
        self.assertEqual(youtube.target, "youtube")

        whatsapp = self.router.route("open wapp")
        self.assertIsNotNone(whatsapp)
        self.assertEqual(whatsapp.action, "open_app")
        self.assertEqual(whatsapp.params["app_name"], "whatsapp")

    def test_routes_telugu_youtube_search_variant(self) -> None:
        intent = self.router.route("youtube lo AI videos search")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, "search_youtube")
        self.assertEqual(intent.params["query"], "ai videos")

    def test_routes_system_power_action(self) -> None:
        intent = self.router.route("shutdown computer")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, "system_action")
        self.assertEqual(intent.params["action_type"], "shutdown")

    def test_routes_direct_youtube_play_request(self) -> None:
        intent = self.router.route("play python tutorials on youtube")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, "play_youtube")
        self.assertEqual(intent.params["query"], "python tutorials")

    def test_open_it_again_prefers_recent_app_context(self) -> None:
        memory = SessionMemory()
        memory.remember_action(
            user_input="open chrome",
            action="open_app",
            target="chrome",
            params={"app_name": "chrome"},
        )
        memory.remember_action(
            user_input="search youtube",
            action="search_web",
            target="youtube",
            params={"query": "youtube", "browser_app": "chrome"},
        )

        intent = self.router.route("open it again", memory=memory.snapshot())

        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, "open_app")
        self.assertEqual(intent.params["app_name"], "chrome")

    def test_search_after_youtube_context_uses_youtube_intent(self) -> None:
        memory = SessionMemory()
        memory.remember_action(
            user_input="open youtube",
            action="open_url",
            target="youtube",
            params={"url": "https://www.youtube.com"},
        )

        parsed = ExecutionPlanner(session_memory=memory).parse("search free fire edits")

        self.assertIsNone(parsed.clarification_question)
        self.assertEqual(len(parsed.intents), 1)
        self.assertEqual(parsed.intents[0].action, "search_youtube")
        self.assertEqual(parsed.intents[0].params["query"], "free fire edits")


if __name__ == "__main__":
    unittest.main()
