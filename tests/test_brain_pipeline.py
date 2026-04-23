from __future__ import annotations

import unittest

from jarvis.config.constants import build_tool_catalog
from jarvis.core.brain import Brain


class StageStubModel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def extract_intent(self, **kwargs):
        del kwargs
        self.calls.append("intent")
        return {
            "intent": "open_app",
            "tool": "open_app",
            "args": {"app_name": "chrome"},
            "confidence": 0.95,
            "clarification_question": None,
            "response": "",
            "unresolved_segments": [],
            "source": "stub",
            "_reasoning_trace": [{"stage": "intent_extraction", "status": "success", "provider": "stub"}],
        }

    def plan_task(self, **kwargs):
        del kwargs
        self.calls.append("plan")
        return {
            "intent": "multi_step_command",
            "tool": "",
            "args": {},
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
                },
                {
                    "tool": "search_web",
                    "target": "youtube",
                    "args": {"query": "youtube", "browser_app": "chrome"},
                    "description": "Search the web for youtube in chrome.",
                    "confidence": 0.92,
                    "depends_on_previous": True,
                    "param_bindings": {},
                },
            ],
            "unresolved_segments": [],
            "source": "stub",
            "_reasoning_trace": [{"stage": "task_planning", "status": "success", "provider": "stub"}],
        }

    def reflect_execution(self, **kwargs):
        del kwargs
        self.calls.append("reflect")
        return {
            "decision": "retry",
            "confidence": 0.8,
            "message": "Chrome failed, so retry in the default browser.",
            "question": None,
            "steps": [
                {
                    "tool": "search_web",
                    "target": "youtube",
                    "args": {"query": "youtube"},
                    "description": "Search the web for youtube.",
                    "confidence": 0.8,
                    "depends_on_previous": False,
                    "param_bindings": {},
                }
            ],
            "unresolved_segments": [],
            "source": "stub",
            "_reasoning_trace": [{"stage": "execution_reflection", "status": "success", "provider": "stub"}],
        }


class BrainPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = StageStubModel()
        self.brain = Brain(llm_client=self.model, tool_catalog=build_tool_catalog())

    def test_understand_runs_intent_then_planning(self) -> None:
        decision = self.brain.understand("open chrome and search youtube")

        self.assertEqual(self.model.calls, ["intent", "plan"])
        self.assertEqual(decision.intent, "multi_step_command")
        self.assertEqual([directive.action for directive in decision.directives], ["open_app", "search_web"])
        self.assertEqual(decision.directives[1].params["browser_app"], "chrome")
        self.assertEqual([trace.stage for trace in decision.reasoning_trace], ["intent_extraction", "task_planning"])

    def test_reflect_returns_structured_recovery(self) -> None:
        reflection = self.brain.reflect(
            "open chrome and search youtube",
            current_plan={"steps": [{"action": "open_app", "target": "chrome"}]},
            failed_step={"action": "open_app", "target": "chrome", "params": {"app_name": "chrome"}},
            execution_result={"success": False, "error": "not_found", "message": "Chrome is unavailable."},
        )

        self.assertEqual(self.model.calls, ["reflect"])
        self.assertEqual(reflection.decision, "retry")
        self.assertEqual(len(reflection.directives), 1)
        self.assertEqual(reflection.directives[0].action, "search_web")
        self.assertEqual([trace.stage for trace in reflection.reasoning_trace], ["execution_reflection"])


if __name__ == "__main__":
    unittest.main()
