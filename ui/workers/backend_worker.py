from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, Signal, Slot

from assistant.command_router import CommandIntent, CommandParse, CommandRouter


_FAST_EXECUTION_ACTIONS = frozenset(
    {
        "close_active_window",
        "close_app",
        "create_file",
        "focus_app",
        "get_clipboard",
        "get_time",
        "health_check",
        "minimize_window",
        "open_explorer",
        "open_app",
        "open_url",
        "read_file",
        "set_clipboard",
        "set_volume",
        "report_capabilities",
        "search_web",
        "switch_window",
    }
)


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


class MockAssistant:
    def handle_text(self, user_input: str):
        text = user_input.strip()
        lowered = text.lower()
        if "time" in lowered:
            response = "Mock mode is active. The backend is unavailable, but the desktop shell is running correctly."
        elif "remember" in lowered:
            response = "Mock mode received your memory request. The real backend is offline, so nothing was persisted."
        elif lowered.startswith(("open ", "launch ", "start ")):
            response = f"Mock mode recognized the command '{text}', but live execution is disabled because the full backend is offline."
        else:
            response = f"Mock mode heard: {text}. The UI is healthy, but the full assistant backend is unavailable."
        snapshot = {"status": "completed", "steps": [], "mode": "mock"}
        return response, snapshot

    def poll_notifications(self):
        return []


class BackendWorker(QObject):
    ready = Signal(object)
    response_ready = Signal(int, str, object)
    stream_update = Signal(int, str, object)
    request_failed = Signal(int, str)
    request_phase_changed = Signal(int, str, object)
    notifications_ready = Signal(object)
    runtime_feedback_ready = Signal(object)
    log = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._assistant = None
        self._mock_mode = False
        self._command_router = CommandRouter()
        self._command_cache: dict[str, CommandIntent] = {}
        self._cancelled_request_ids: set[int] = set()
        self._runtime_events_connected = False

    @Slot()
    def initialize(self) -> None:
        try:
            if self._assistant is not None:
                self.ready.emit(
                    {
                        "status": "Mock" if self._mock_mode else "Ready",
                        "mock": self._mock_mode,
                        "safe_mode": self._current_safe_mode(),
                    }
                )
                return

            if os.getenv("JARVIS_FORCE_MOCK_BACKEND", "").lower() in {"1", "true", "yes"}:
                self._assistant = MockAssistant()
                self._mock_mode = True
                self.ready.emit({"status": "Mock", "mock": True, "safe_mode": False})
                self.log.emit("Backend", "Forced mock backend enabled.")
                return

            try:
                from assistant.app import build_assistant

                orchestrator = build_assistant()

                try:
                    from assistant.hybrid_engine import HybridEngine

                    self._assistant = HybridEngine(orchestrator)
                    self.log.emit("Backend", "Hybrid engine initialized (local + online routing).")
                except Exception as hybrid_exc:
                    self.log.emit("Backend", f"HybridEngine init failed, using raw orchestrator: {hybrid_exc}")
                    self._assistant = orchestrator

                self._mock_mode = False
                self._subscribe_runtime_events()
                self.ready.emit({"status": "Ready", "safe_mode": self._current_safe_mode()})
                self.log.emit("Backend", "Assistant backend initialized.")
            except Exception as exc:
                self._assistant = MockAssistant()
                self._mock_mode = True
                self.ready.emit({"status": "Mock", "error": str(exc), "mock": True, "safe_mode": False})
                self.log.emit("Backend", f"Backend unavailable, starting mock mode: {exc}")
        except Exception as exc:
            self.ready.emit({"status": "Error", "error": f"Critical backend failure: {exc}"})

    @Slot(int, str)
    def process(self, request_id: int, transcript: str) -> None:
        started_at = time.perf_counter()
        stream_callback = None
        try:
            if self._assistant is None:
                self.initialize()
            if self._assistant is None:
                self.request_failed.emit(request_id, "Backend could not be initialized, even in mock mode.")
                return

            cleaned = transcript.strip()
            if not cleaned:
                self.log.emit("Backend", "[BACKEND] discarded empty text.")
                self.request_failed.emit(request_id, "Empty transcript discarded.")
                return

            self.log.emit("Backend", f"[BACKEND] received text: {cleaned}")
            self._cancelled_request_ids.discard(request_id)
            if hasattr(self._assistant, "begin_execution"):
                self._assistant.begin_execution(request_id)
            self.request_phase_changed.emit(
                request_id,
                "PROCESSING",
                {"mode": "text", "route": "assistant"},
            )

            handle_started_at = time.perf_counter()
            self.log.emit("Backend", f"[BACKEND] execution started for: {cleaned}")
            if hasattr(self._assistant, "subscribe_stream"):
                def _stream_callback(update, rid=request_id):
                    self.stream_update.emit(rid, update.text, {"task_id": update.task_id, "final": update.final, "lines": list(update.lines)})

                stream_callback = _stream_callback
                self._assistant.subscribe_stream(stream_callback)
            response, snapshot = self._assistant.handle_text(cleaned)
            self._cache_safe_command(snapshot)
            handle_elapsed_ms = (time.perf_counter() - handle_started_at) * 1000.0
            total_elapsed_ms = (time.perf_counter() - started_at) * 1000.0

            self.log.emit("Backend", f"[BACKEND] response sent: {response}")
            self.log.emit(
                "Timing",
                f"Backend handled request in {handle_elapsed_ms:.1f} ms (total {total_elapsed_ms:.1f} ms)",
            )
            self.response_ready.emit(request_id, response, snapshot)
            notifications = self._assistant.poll_notifications()
            if notifications:
                self.notifications_ready.emit(notifications)
        except Exception as exc:
            self.request_failed.emit(request_id, f"Backend request failed: {exc}")
        finally:
            if stream_callback is not None and hasattr(self._assistant, "unsubscribe_stream"):
                self._assistant.unsubscribe_stream(stream_callback)
            if hasattr(self._assistant, "finish_execution"):
                self._assistant.finish_execution(request_id)

    def cancel_request_now(self, request_id: int | None = None) -> bool:
        if request_id is not None:
            self._cancelled_request_ids.add(request_id)
        if self._assistant is None:
            return False
        if hasattr(self._assistant, "cancel_active"):
            return bool(self._assistant.cancel_active(request_id))
        return False

    def _subscribe_runtime_events(self) -> None:
        if self._assistant is None or self._runtime_events_connected:
            return
        if not hasattr(self._assistant, "subscribe_runtime_event"):
            return

        self._assistant.subscribe_runtime_event("execution.feedback", self._on_execution_feedback)
        self._assistant.subscribe_runtime_event("safety.status", self._on_safety_status)
        self._assistant.subscribe_runtime_event("execution.finalized", self._on_execution_finalized)
        self._runtime_events_connected = True

    def _on_execution_feedback(self, payload: dict[str, object]) -> None:
        feedback = payload.get("feedback") if isinstance(payload, dict) else None
        if feedback is None:
            return
        self.runtime_feedback_ready.emit(
            {
                "type": "activity",
                "activity": str(getattr(feedback, "message", "") or getattr(feedback, "description", "")).strip(),
            }
        )

    def _on_safety_status(self, payload: dict[str, object]) -> None:
        if not isinstance(payload, dict):
            return
        self.runtime_feedback_ready.emit(
            {
                "type": "safety",
                "safety_level": str(payload.get("safety_level", "SAFE")).strip() or "SAFE",
                "reason": str(payload.get("reason", "")).strip(),
                "activity": str(payload.get("description", "")).strip(),
                "scope": str(payload.get("scope", "single")).strip() or "single",
            }
        )

    def _on_execution_finalized(self, payload: dict[str, object]) -> None:
        if not isinstance(payload, dict):
            return
        self.runtime_feedback_ready.emit(
            {
                "type": "finalized",
                "activity": str(payload.get("status", "completed")).strip().title(),
                "reason": str(payload.get("response", "")).strip(),
            }
        )

    @Slot(bool)
    def set_safe_mode(self, enabled: bool) -> None:
        try:
            if self._assistant is None or self._mock_mode:
                return
            orchestrator = self._resolve_orchestrator()
            if orchestrator is None:
                return
            engine = getattr(orchestrator, "_action_engine", None)
            if engine is None:
                return
            context = getattr(engine, "_context", None)
            if context is None:
                return
            context.settings.safe_mode = enabled
            mode = "ON" if enabled else "OFF"
            print(f"[SAFE MODE] Safe mode set to {mode}")
            self.log.emit(
                "Backend",
                f"Safe mode set to {mode}. simulate_actions remains {'ON' if context.settings.simulate_actions else 'OFF'}.",
            )
        except Exception as exc:
            self.log.emit("Backend", f"Failed to set safe mode: {exc}")

    def _resolve_orchestrator(self):
        assistant = self._assistant
        if assistant is None:
            return None

        orchestrator = getattr(assistant, "_orchestrator", None)
        if orchestrator is not None:
            return orchestrator

        local_handler = getattr(assistant, "_local", None)
        if local_handler is None:
            return None
        return getattr(local_handler, "_orchestrator", None)

    def _resolve_planner(self):
        orchestrator = self._resolve_orchestrator()
        if orchestrator is None:
            return None
        return getattr(orchestrator, "_planner", None)

    def _resolve_action_engine(self):
        orchestrator = self._resolve_orchestrator()
        if orchestrator is None:
            return None
        return getattr(orchestrator, "_action_engine", None)

    def _current_safe_mode(self) -> bool:
        engine = self._resolve_action_engine()
        if engine is None:
            return False
        context = getattr(engine, "_context", None)
        settings = getattr(context, "settings", None)
        return bool(getattr(settings, "safe_mode", False))

    def _execute_fast_command(
        self,
        transcript: str,
        intent: CommandIntent,
    ) -> tuple[str, dict[str, object]] | None:
        del transcript, intent
        return None

    def _parse_fast_command(self, cleaned: str, normalized: str) -> CommandParse | None:
        cached_intent = self._command_cache.get(normalized)
        if cached_intent is not None:
            return CommandParse(intents=[cached_intent], confidence=cached_intent.confidence, normalized_text=normalized)

        planner = self._resolve_planner()
        if planner is not None and hasattr(planner, "parse_command"):
            return planner.parse_command(cleaned)
        memory = planner.session_snapshot() if planner is not None and hasattr(planner, "session_snapshot") else None
        return self._command_router.route_many(cleaned, memory=memory)

    def _cache_safe_command(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return
        if snapshot.get("status") != "completed":
            return

        steps = snapshot.get("steps")
        if not isinstance(steps, list) or len(steps) != 1:
            return

        step = steps[0]
        if not isinstance(step, dict):
            return

        action = str(step.get("action", "")).strip()
        if action not in _FAST_EXECUTION_ACTIONS:
            return

        goal_text = _normalize_text(str(snapshot.get("user_input", "")).strip())
        if not goal_text:
            return

        params = dict(step.get("params") or {})
        if action == "open_url" and "url" not in params:
            result = step.get("result")
            if isinstance(result, dict) and result.get("url"):
                params["url"] = result["url"]
        if action == "search_web" and "query" not in params:
            target = str(step.get("target", "")).strip()
            if target:
                params["query"] = target
        if action == "open_app" and "app_name" not in params:
            target = str(step.get("target", "")).strip()
            if target:
                params["app_name"] = target
        if action in {"focus_app", "close_app"} and "app_name" not in params:
            target = str(step.get("target", "")).strip()
            if target:
                params["app_name"] = target
        if action in {"create_file", "read_file", "delete_file", "overwrite_file"} and "name" not in params:
            target = str(step.get("target", "")).strip()
            if target:
                params["name"] = target
        if action == "set_volume" and "value" not in params:
            target = str(step.get("target", "")).strip()
            if target:
                params["value"] = target
        if action == "open_explorer" and "path" not in params:
            target = str(step.get("target", "")).strip()
            if target:
                params["path"] = target
        if action == "set_clipboard" and "text" not in params:
            target = str(step.get("target", "")).strip()
            if target:
                params["text"] = target
        if action == "switch_window" and "title" not in params:
            target = str(step.get("target", "")).strip()
            if target:
                params["title"] = target

        self._command_cache[goal_text] = CommandIntent(
            action=action,
            target=str(step.get("target", "")).strip(),
            params=params,
            description=str(step.get("description", action)).strip(),
        )
