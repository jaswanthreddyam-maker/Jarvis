from __future__ import annotations

import json
import unittest

from jarvis.application.runtime_support import SessionContextStore
from jarvis.config.constants import build_tool_catalog
from jarvis.infrastructure.ai.llm_client import LLMClient
from jarvis.infrastructure.ai.online_provider import OnlineResponse


class QueueProvider:
    def __init__(self, responses: list[OnlineResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def query(self, user_text: str, context=None, **kwargs) -> OnlineResponse:
        self.calls.append({"user_text": user_text, "context": context, "kwargs": kwargs})
        if not self._responses:
            raise AssertionError("No queued provider response available.")
        return self._responses.pop(0)


def _extract_request_payload(prompt_text: object) -> dict[str, object]:
    text = str(prompt_text)
    marker = "INPUT_JSON:"
    if marker not in text:
        raise AssertionError(f"Missing INPUT_JSON marker in prompt: {text}")
    payload_text = text.split(marker, 1)[1].strip()
    return json.loads(payload_text)


class LLMClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool_catalog = build_tool_catalog()

    def test_extract_intent_accepts_valid_json(self) -> None:
        provider = QueueProvider(
            [
                OnlineResponse(
                    text=json.dumps(
                        {
                            "intent": "open_app",
                            "tool": "open_app",
                            "args": {"app_name": "chrome"},
                            "confidence": 0.96,
                            "clarification_question": None,
                            "response": "",
                            "unresolved_segments": [],
                        }
                    ),
                    provider="stub",
                )
            ]
        )

        payload = LLMClient(provider=provider).extract_intent(
            user_text="open chrome",
            normalized_text="open chrome",
            tool_catalog=self.tool_catalog,
        )

        self.assertEqual(payload["intent"], "open_app")
        self.assertEqual(payload["tool"], "open_app")
        self.assertEqual(payload["args"]["app_name"], "chrome")
        self.assertEqual(payload["_reasoning_trace"][0]["stage"], "intent_extraction")
        self.assertIn("not a chatbot", str(provider.calls[0]["kwargs"]["system_prompt"]).lower())

    def test_plan_task_normalizes_single_step_top_level_payload(self) -> None:
        provider = QueueProvider(
            [
                OnlineResponse(
                    text=json.dumps(
                        {
                            "intent": "search_web",
                            "tool": "search_web",
                            "args": {"query": "jarvis architecture"},
                            "confidence": 0.91,
                            "clarification_question": None,
                            "response": "",
                            "steps": [],
                            "unresolved_segments": [],
                        }
                    ),
                    provider="stub",
                )
            ]
        )

        payload = LLMClient(provider=provider).plan_task(
            user_text="search jarvis architecture",
            normalized_text="search jarvis architecture",
            tool_catalog=self.tool_catalog,
            intent_payload={"intent": "search_web", "tool": "search_web", "args": {"query": "jarvis architecture"}},
        )

        self.assertEqual(len(payload["steps"]), 1)
        self.assertEqual(payload["steps"][0]["tool"], "search_web")
        self.assertEqual(payload["args"]["query"], "jarvis architecture")

    def test_parse_command_uses_intent_then_plan(self) -> None:
        provider = QueueProvider(
            [
                OnlineResponse(
                    text=json.dumps(
                        {
                            "intent": "open_app",
                            "tool": "open_app",
                            "args": {"app_name": "chrome"},
                            "confidence": 0.95,
                            "clarification_question": None,
                            "response": "",
                            "unresolved_segments": [],
                        }
                    ),
                    provider="stub",
                ),
                OnlineResponse(
                    text=json.dumps(
                        {
                            "intent": "open_app",
                            "tool": "open_app",
                            "args": {"app_name": "chrome"},
                            "confidence": 0.95,
                            "clarification_question": None,
                            "response": "",
                            "steps": [
                                {
                                    "tool": "open_app",
                                    "target": "chrome",
                                    "args": {"app_name": "chrome"},
                                    "description": "Open chrome.",
                                    "confidence": 0.95,
                                    "depends_on_previous": False,
                                    "param_bindings": {},
                                }
                            ],
                            "unresolved_segments": [],
                        }
                    ),
                    provider="stub",
                ),
            ]
        )

        payload = LLMClient(provider=provider).parse_command(
            user_text="open chrome",
            normalized_text="open chrome",
            tool_catalog=self.tool_catalog,
        )

        self.assertEqual(payload["intent"], "open_app")
        self.assertEqual(len(provider.calls), 2)
        first_payload = _extract_request_payload(provider.calls[0]["user_text"])
        second_payload = _extract_request_payload(provider.calls[1]["user_text"])
        self.assertEqual(first_payload["stage"], "intent_extraction")
        self.assertEqual(second_payload["stage"], "task_planning")
        self.assertEqual(second_payload["intent_summary"]["tool"], "open_app")
        self.assertIn("reasoning controller", str(provider.calls[0]["kwargs"]["system_prompt"]).lower())

    def test_prompt_payload_includes_recent_context(self) -> None:
        provider = QueueProvider(
            [
                OnlineResponse(
                    text=json.dumps(
                        {
                            "intent": "play_youtube",
                            "tool": "play_youtube",
                            "args": {"query": "python tutorials"},
                            "confidence": 0.88,
                            "clarification_question": None,
                            "response": "",
                            "unresolved_segments": [],
                        }
                    ),
                    provider="stub",
                )
            ]
        )
        memory = SessionContextStore()
        memory.remember_action(
            user_input="search python tutorials",
            action="search_youtube",
            target="python tutorials",
            params={"query": "python tutorials"},
        )
        session_snapshot = memory.snapshot()
        memory_payload = {
            **session_snapshot.as_payload(),
            "short_term": [{"user": "play music", "assistant": "What kind of music?"}],
            "long_term": {"preferences": {"favorite_music": "lofi"}},
            "semantic": [{"text": "User prefers lofi playlists while coding.", "score": 0.92}],
            "write_policy": {"store_only": ["preferences"]},
        }

        LLMClient(provider=provider).extract_intent(
            user_text="play it",
            normalized_text="play it",
            tool_catalog=self.tool_catalog,
            memory=memory_payload,
            conversation=[{"role": "user", "content": "search python tutorials"}],
            system_state={"last_action_success": True},
        )

        request_payload = _extract_request_payload(provider.calls[0]["user_text"])
        self.assertEqual(request_payload["stage"], "intent_extraction")
        self.assertEqual(request_payload["session_context"]["last_action"], "search_youtube")
        self.assertEqual(request_payload["session_context"]["last_target"], "python tutorials")
        self.assertEqual(request_payload["conversation_context"][0]["content"], "search python tutorials")
        self.assertTrue(request_payload["system_state"]["last_action_success"])
        self.assertEqual(request_payload["memory_context"]["long_term"]["preferences"]["favorite_music"], "lofi")


if __name__ == "__main__":
    unittest.main()
