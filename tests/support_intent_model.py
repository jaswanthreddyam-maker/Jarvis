from __future__ import annotations

import re
from typing import Any


class ScriptedIntentModel:
    """Deterministic test double for the brain's staged reasoning pipeline."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def extract_intent(
        self,
        *,
        user_text: str,
        normalized_text: str,
        tool_catalog,
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del tool_catalog, conversation, system_state
        self.calls.append("intent")
        payload = self._plan_payload(normalized_text, memory=memory)
        if payload["intent"] == "unknown":
            return {
                "intent": "unknown",
                "tool": "",
                "args": {},
                "confidence": payload["confidence"],
                "clarification_question": payload["clarification_question"],
                "response": payload["response"],
                "unresolved_segments": list(payload["unresolved_segments"]),
                "_reasoning_trace": [{"stage": "intent_extraction", "status": "success", "provider": "stub"}],
            }

        first_step = payload["steps"][0] if payload["steps"] else {}
        intent_name = payload["intent"]
        if intent_name == "multi_step_command":
            intent_name = str(first_step.get("tool", "multi_step_command"))
        return {
            "intent": intent_name,
            "tool": str(first_step.get("tool", "")),
            "args": dict(first_step.get("args", {})),
            "confidence": payload["confidence"],
            "clarification_question": None,
            "response": "",
            "unresolved_segments": [],
            "_reasoning_trace": [{"stage": "intent_extraction", "status": "success", "provider": "stub"}],
        }

    def plan_task(
        self,
        *,
        user_text: str,
        normalized_text: str,
        tool_catalog,
        intent_payload: dict[str, Any],
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del user_text, tool_catalog, intent_payload, conversation, system_state
        self.calls.append("plan")
        payload = self._plan_payload(normalized_text, memory=memory)
        payload["_reasoning_trace"] = [{"stage": "task_planning", "status": "success", "provider": "stub"}]
        return payload

    def reflect_execution(
        self,
        *,
        user_text: str,
        normalized_text: str,
        tool_catalog,
        current_plan: dict[str, Any],
        failed_step: dict[str, Any],
        execution_result: dict[str, Any],
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
        system_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del user_text, normalized_text, tool_catalog, current_plan, memory, conversation, system_state
        self.calls.append("reflect")
        action = str(failed_step.get("action", "")).strip().lower()
        target = str(failed_step.get("target", "")).strip()
        error = str(execution_result.get("error", "")).strip().lower()

        if action == "open_app" and target.lower() in {"chrome", "firefox"}:
            return {
                "decision": "replan",
                "confidence": 0.74,
                "message": f"{target} failed to launch. Should I use your default browser instead?",
                "question": f"{target} failed to launch. Should I use your default browser instead?",
                "steps": [
                    self._step("search_web", {"query": "youtube"}, target="youtube")
                ],
                "unresolved_segments": [],
                "_reasoning_trace": [{"stage": "execution_reflection", "status": "success", "provider": "stub"}],
            }

        if action == "play_youtube" and error == "verification_failed":
            query = str(failed_step.get("params", {}).get("query") or target).strip()
            return {
                "decision": "replan",
                "confidence": 0.82,
                "message": f"Playback was not confirmed, so I can safely open YouTube results for {query}.",
                "question": f"Playback was not confirmed for {query}. Should I open the YouTube results instead?",
                "steps": [
                    self._step("open_url", {"url": "https://www.youtube.com"}, target="youtube"),
                    self._step("search_youtube", {"query": query}, target=query, depends_on_previous=True),
                ],
                "unresolved_segments": [],
                "_reasoning_trace": [{"stage": "execution_reflection", "status": "success", "provider": "stub"}],
            }

        return {
            "decision": "abort",
            "confidence": 0.5,
            "message": "I couldn't find a better recovery for that failure.",
            "question": None,
            "steps": [],
            "unresolved_segments": [],
            "_reasoning_trace": [{"stage": "execution_reflection", "status": "success", "provider": "stub"}],
        }

    def parse_command(
        self,
        *,
        user_text: str,
        normalized_text: str,
        tool_catalog,
        memory: Any | None = None,
        conversation: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        intent_payload = self.extract_intent(
            user_text=user_text,
            normalized_text=normalized_text,
            tool_catalog=tool_catalog,
            memory=memory,
            conversation=conversation,
        )
        if intent_payload.get("clarification_question"):
            return {
                **intent_payload,
                "steps": [],
            }
        return self.plan_task(
            user_text=user_text,
            normalized_text=normalized_text,
            tool_catalog=tool_catalog,
            intent_payload=intent_payload,
            memory=memory,
            conversation=conversation,
        )

    def _plan_payload(self, normalized_text: str, *, memory: Any | None) -> dict[str, Any]:
        snapshot = memory.snapshot() if hasattr(memory, "snapshot") else memory
        recent_contexts = list(self._snapshot_value(snapshot, "recent_contexts") or []) if snapshot is not None else []
        last_command = str(self._snapshot_value(snapshot, "last_command") or "").strip().lower()
        last_target = str(self._snapshot_value(snapshot, "last_target") or "").strip()
        last_app = str(self._snapshot_value(snapshot, "last_app") or "").strip().lower()
        last_params = dict(self._snapshot_value(snapshot, "last_params") or {}) if snapshot is not None else {}

        text = " ".join(normalized_text.strip().lower().split())
        if not text:
            return self._unknown("Can you clarify what you want me to do?", unresolved=[text])

        direct = {
            "can you open chrome": self._single("open_app", {"app_name": "chrome"}, target="chrome"),
            "open chrome": self._single("open_app", {"app_name": "chrome"}, target="chrome"),
            "open chroem": self._unknown("Did you mean Chrome?", unresolved=["chroem"]),
            "open firefox": self._single("open_app", {"app_name": "firefox"}, target="firefox"),
            "open youtube": self._single("open_url", {"url": "https://www.youtube.com"}, target="youtube"),
            "open yt": self._single("open_url", {"url": "https://www.youtube.com"}, target="youtube"),
            "open wapp": self._single("open_app", {"app_name": "whatsapp"}, target="whatsapp"),
            "show downloads folder": self._single("open_explorer", {"path": "downloads"}, target="downloads"),
            "increase volume": self._single("set_volume", {"value": "increase"}, target="increase"),
            "what is on the clipboard": self._single("get_clipboard", {}, target="clipboard"),
            "what's on the clipboard": self._single("get_clipboard", {}, target="clipboard"),
            "shutdown computer": self._single("system_action", {"action_type": "shutdown"}, target="shutdown"),
            "open something": self._unknown("What do you want me to open?", unresolved=["open something"]),
            "search youtube and spotify": self._unknown(
                "I found multiple platforms in that request: YouTube and Spotify. Which one should I use?",
                unresolved=["youtube", "spotify"],
            ),
        }
        if text in direct:
            return direct[text]

        create_match = re.fullmatch(r"create a file named (.+)", text)
        if create_match:
            name = create_match.group(1).strip()
            return self._single("create_file", {"name": name}, target=name)

        remember_match = re.fullmatch(r"remember that (.+)", text)
        if remember_match:
            content = remember_match.group(1).strip()
            return self._single(
                "remember_fact",
                {"content": content, "namespace": "system", "category": "fact"},
                target=content,
            )

        recall_match = re.fullmatch(r"what do you remember about (.+)", text)
        if recall_match:
            query = recall_match.group(1).strip()
            return self._single("recall_memory", {"query": query, "namespace": "system"}, target=query)

        reminder_match = re.fullmatch(
            r"remind me to (.+?) in (\d+) (second|seconds|minute|minutes|hour|hours)",
            text,
        )
        if reminder_match:
            message = reminder_match.group(1).strip()
            amount = int(reminder_match.group(2))
            unit = reminder_match.group(3)
            delay_seconds = self._to_seconds(amount, unit)
            return self._single(
                "set_reminder",
                {"message": message, "delay_seconds": delay_seconds},
                target=message,
            )

        delete_match = re.fullmatch(r"delete file (.+)", text)
        if delete_match:
            name = delete_match.group(1).strip()
            return self._single("delete_file", {"name": name}, target=name)

        youtube_search_match = re.fullmatch(r"search (.+) on youtube", text)
        if youtube_search_match:
            query = youtube_search_match.group(1).strip()
            return self._single("search_youtube", {"query": query}, target=query)

        telugu_youtube_search_match = re.fullmatch(r"youtube lo (.+) search", text)
        if telugu_youtube_search_match:
            query = telugu_youtube_search_match.group(1).strip()
            return self._single("search_youtube", {"query": query}, target=query)

        youtube_play_match = re.fullmatch(r"play (.+) on youtube", text)
        if youtube_play_match:
            query = youtube_play_match.group(1).strip()
            return self._single("play_youtube", {"query": query}, target=query)

        if text == "open chrome and search youtube":
            return self._multi(
                self._step("open_app", {"app_name": "chrome"}, target="chrome"),
                self._step(
                    "search_web",
                    {"query": "youtube", "browser_app": "chrome"},
                    target="youtube",
                    depends_on_previous=True,
                ),
            )

        youtube_chain_match = re.fullmatch(r"open youtube and search (.+)", text)
        if youtube_chain_match:
            query = youtube_chain_match.group(1).strip()
            return self._multi(
                self._step("open_url", {"url": "https://www.youtube.com"}, target="youtube"),
                self._step("search_youtube", {"query": query}, target=query, depends_on_previous=True),
            )

        if text in {"open it again", "open that again", "again"}:
            for record in recent_contexts:
                action = str(getattr(record, "action", "")).strip().lower()
                params = dict(getattr(record, "params", {}) or {})
                target = str(getattr(record, "target", "")).strip()
                if action != "open_app":
                    continue
                app_name = str(params.get("app_name") or target).strip()
                if app_name:
                    return self._single("open_app", {"app_name": app_name}, target=app_name)
            if last_command == "open_url":
                target = last_target or "youtube"
                url = str(last_params.get("url") or "https://www.youtube.com").strip()
                return self._single("open_url", {"url": url}, target=target)
            if last_command == "open_app":
                app_name = str(last_params.get("app_name") or last_target).strip()
                return self._single("open_app", {"app_name": app_name}, target=app_name)
            return self._unknown("What should I repeat?", unresolved=[text])

        if text == "search youtube":
            recent_browsers = {
                str(getattr(record, "primary_app", "")).strip().lower()
                for record in recent_contexts[:3]
                if str(getattr(record, "primary_app", "")).strip().lower() in {"chrome", "firefox", "edge", "comet"}
            }
            if len(recent_browsers) > 1:
                choices = " or ".join(browser.title() for browser in sorted(recent_browsers))
                return self._unknown(
                    f"I found multiple browsers in recent context: {choices}. Which one should I use?",
                    unresolved=["search youtube"],
                )
            if last_app == "youtube":
                return self._single("search_youtube", {"query": "youtube"}, target="youtube")
            return self._single("search_web", {"query": "youtube"}, target="youtube")

        generic_search_match = re.fullmatch(r"search (.+)", text)
        if generic_search_match:
            query = generic_search_match.group(1).strip()
            if last_app == "youtube" or self._recent_surface(recent_contexts) == "youtube":
                return self._single("search_youtube", {"query": query}, target=query)
            return self._single("search_web", {"query": query}, target=query)

        if text == "play it":
            query = self._latest_youtube_query(recent_contexts)
            if query:
                return self._single("play_youtube", {"query": query}, target=query)
            return self._unknown("What should I play?", unresolved=["play it"])

        return self._unknown(
            "I couldn't turn that into a valid tool request yet. Could you rephrase it a little more explicitly?",
            unresolved=[text],
        )

    @staticmethod
    def _recent_surface(recent_contexts: list[Any]) -> str:
        for record in recent_contexts:
            primary_app = str(getattr(record, "primary_app", "")).strip().lower()
            if primary_app:
                return primary_app
        return ""

    @staticmethod
    def _latest_youtube_query(recent_contexts: list[Any]) -> str:
        for record in recent_contexts:
            action = str(getattr(record, "action", "")).strip().lower()
            params = dict(getattr(record, "params", {}) or {})
            primary_app = str(getattr(record, "primary_app", "")).strip().lower()
            if action in {"search_youtube", "play_youtube"} or primary_app == "youtube":
                query = str(params.get("query") or getattr(record, "target", "") or "").strip()
                if query:
                    return query
        return ""

    @staticmethod
    def _to_seconds(amount: int, unit: str) -> int:
        if unit.startswith("hour"):
            return amount * 3600
        if unit.startswith("minute"):
            return amount * 60
        return amount

    def _single(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        target: str = "",
        confidence: float = 0.95,
    ) -> dict[str, Any]:
        return {
            "intent": tool,
            "tool": tool,
            "args": dict(args),
            "confidence": confidence,
            "clarification_question": None,
            "response": "",
            "steps": [self._step(tool, args, target=target)],
            "unresolved_segments": [],
        }

    def _multi(self, *steps: dict[str, Any], confidence: float = 0.95) -> dict[str, Any]:
        return {
            "intent": "multi_step_command",
            "tool": "",
            "args": {},
            "confidence": confidence,
            "clarification_question": None,
            "response": "",
            "steps": list(steps),
            "unresolved_segments": [],
        }

    @staticmethod
    def _unknown(question: str, *, unresolved: list[str]) -> dict[str, Any]:
        return {
            "intent": "unknown",
            "tool": "",
            "args": {},
            "confidence": 0.2,
            "clarification_question": question,
            "response": question,
            "steps": [],
            "unresolved_segments": list(unresolved),
        }

    @staticmethod
    def _step(
        tool: str,
        args: dict[str, Any],
        *,
        target: str = "",
        depends_on_previous: bool = False,
        confidence: float = 0.95,
    ) -> dict[str, Any]:
        resolved_target = target or str(
            args.get("query")
            or args.get("url")
            or args.get("app_name")
            or args.get("name")
            or args.get("path")
            or args.get("action_type")
            or ""
        ).strip()
        return {
            "tool": tool,
            "target": resolved_target,
            "args": dict(args),
            "description": tool.replace("_", " "),
            "confidence": confidence,
            "depends_on_previous": depends_on_previous,
            "param_bindings": {},
        }

    @staticmethod
    def _snapshot_value(snapshot: Any, key: str) -> Any:
        if snapshot is None:
            return None
        if isinstance(snapshot, dict):
            return snapshot.get(key)
        return getattr(snapshot, key, None)
