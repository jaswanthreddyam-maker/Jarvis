"""Shared task context for conversational follow-ups and chained execution."""
from __future__ import annotations

import logging
import threading
import time
from copy import deepcopy
from typing import Any

logger = logging.getLogger("Jarvis.TaskContext")

_YOUTUBE_URL_HINTS = ("youtube.com", "youtu.be")


class TaskContext:
    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Conversational follow-up context.
        self._current_task: str | None = None
        self._task_data: dict[str, Any] = {}
        self._expecting_reply: bool = False
        self._expires_at: float = 0.0

        # Goal-execution context.
        self._execution_context = self._new_execution_context()

    @staticmethod
    def _new_execution_context() -> dict[str, Any]:
        return {
            "active_app": None,
            "active_app_source": None,
            "last_action_success": False,
            "workflow": [],
        }

    def set_context(
        self,
        task_name: str,
        data: dict[str, Any] | None = None,
        expecting_reply: bool = False,
        timeout_sec: int = 60,
    ) -> None:
        """Set the active conversational follow-up context."""
        with self._lock:
            self._current_task = task_name
            self._task_data = data or {}
            self._expecting_reply = expecting_reply
            self._expires_at = time.time() + timeout_sec if expecting_reply else 0.0
            logger.debug(
                "Context set to: %s (expecting reply: %s, expires in %ds)",
                task_name,
                expecting_reply,
                timeout_sec,
            )

    def get_context(self) -> tuple[str | None, dict[str, Any], bool]:
        """Get the active conversational context."""
        with self._lock:
            if self._expecting_reply and time.time() > self._expires_at:
                logger.debug("Task context expired: %s", self._current_task)
                self._clear_conversation_unsafe()
            return self._current_task, dict(self._task_data), self._expecting_reply

    def start_workflow(self, *, active_app: str | None = None) -> None:
        """Reset and start a shared execution workflow context."""
        with self._lock:
            self._execution_context = self._new_execution_context()
            if active_app:
                self._execution_context["active_app"] = active_app.strip().lower() or None

    def clear_workflow(self) -> None:
        with self._lock:
            self._execution_context = self._new_execution_context()

    def workflow_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_app": self._execution_context["active_app"],
                "active_app_source": self._execution_context["active_app_source"],
                "last_action_success": bool(self._execution_context["last_action_success"]),
                "workflow": [deepcopy(step) for step in self._execution_context["workflow"]],
            }

    @staticmethod
    def build_result_contract(
        *,
        success: bool,
        action: str,
        target: str = "",
        data: dict[str, Any] | None = None,
        error: str | None = None,
        verified: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": success,
            "action": action,
            "target": target,
            "verified": verified,
        }
        if data:
            payload["data"] = dict(data)
        if error:
            payload["error"] = error
        return payload

    def record_result(
        self,
        *,
        intent: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Update shared execution state from an action result contract."""
        with self._lock:
            success = bool(result.get("success"))
            verified = bool(result.get("verified") or dict(result.get("data", {})).get("verified"))
            self._execution_context["last_action_success"] = success and verified

            if success and verified:
                workflow_step = {
                    "intent": str(intent.get("intent") or result.get("action") or "").strip(),
                    "target": str(result.get("target") or intent.get("target") or "").strip(),
                    "params": deepcopy(intent.get("params", {})),
                    "result": deepcopy(result.get("data", {})),
                }
                self._execution_context["workflow"].append(workflow_step)

            active_app = self._infer_active_app(intent=intent, result=result)
            if active_app:
                self._execution_context["active_app"] = active_app
                self._execution_context["active_app_source"] = str(
                    dict(result.get("data", {})).get("active_app_source") or ""
                ).strip() or None

    def active_app(self) -> str | None:
        with self._lock:
            return self._execution_context["active_app"]

    def _clear_conversation_unsafe(self) -> None:
        self._current_task = None
        self._task_data = {}
        self._expecting_reply = False
        self._expires_at = 0.0

    def clear(self) -> None:
        """Clear conversational and execution context."""
        with self._lock:
            self._clear_conversation_unsafe()
            self._execution_context = self._new_execution_context()
            logger.debug("Context cleared")

    def handle_reply(self, user_text: str) -> dict[str, Any] | None:
        """Process a reply based on the active conversational context."""
        with self._lock:
            if not self._expecting_reply or not self._current_task:
                return None

            if time.time() > self._expires_at:
                logger.debug("Task context expired before reply: %s", self._current_task)
                self._clear_conversation_unsafe()
                return None

            task = self._current_task
            data = self._task_data

            lowered = user_text.lower()
            is_positive = any(word in lowered for word in ["yes", "yeah", "sure", "ok", "please", "do it"])
            is_negative = any(word in lowered for word in ["no", "nah", "stop", "cancel"])

            self._expecting_reply = False

            if task == "youtube_opened":
                if is_positive:
                    return {"action": "ask_query", "message": "What should I search for?"}
                query = lowered.replace("search for", "").strip()
                return {"action": "youtube_search", "query": query}

            if task == "app_install" and is_positive:
                return {"action": "proceed_install", "app": data.get("app")}

            if task == "execution_feedback":
                if is_positive:
                    return {"action": "feedback_success", "category": data.get("category")}
                if is_negative:
                    return {
                        "action": "feedback_failure",
                        "category": data.get("category"),
                        "rollback_cmd": data.get("rollback_cmd"),
                    }

            if task == "admin_relaunch_prompt" and is_positive:
                return {"action": "relaunch_admin"}

            if is_negative:
                logger.info("User cancelled follow-up for %s", task)
                self._clear_conversation_unsafe()
                return {"action": "cancel", "message": "Okay, standing by."}

            return None

    @staticmethod
    def _infer_active_app(*, intent: dict[str, Any], result: dict[str, Any]) -> str | None:
        action = str(result.get("action") or intent.get("intent") or "").strip().lower()
        target = str(result.get("target") or intent.get("target") or "").strip().lower()
        params = dict(intent.get("params", {}))
        data = dict(result.get("data", {}))
        verified = bool(result.get("verified") or data.get("verified"))

        if not verified:
            return None

        explicit = str(data.get("active_app") or "").strip().lower()
        if explicit:
            return explicit

        url = str(data.get("url") or data.get("search_url") or params.get("url") or "").strip().lower()
        if action in {"open_url", "search_youtube", "play_youtube"} or any(hint in url for hint in _YOUTUBE_URL_HINTS):
            if "youtube" in target or any(hint in url for hint in _YOUTUBE_URL_HINTS):
                return "youtube"

        if action == "open_app" and target:
            return target
        if action == "search_web":
            browser_app = str(data.get("browser_app") or params.get("browser_app") or "").strip().lower()
            if browser_app:
                return browser_app

        return None


task_context = TaskContext()
