from __future__ import annotations

from typing import Any

from jarvis.application.runtime_support import ReminderScheduler, SessionContextStore
from jarvis.application.streaming import chunk_response_text
from jarvis.core.cancellation import CancellationController
from jarvis.core.executor import ToolExecutor
from jarvis.core.memory import MemoryManager
from jarvis.core.planner import Planner


class JarvisOrchestrator:
    def __init__(
        self,
        *,
        planner: Planner,
        executor: ToolExecutor,
        memory: MemoryManager,
        settings,
        event_bus=None,
        scheduler: ReminderScheduler | None = None,
        session_context: SessionContextStore | None = None,
        health_service: Any | None = None,
        cancellation_controller: CancellationController | None = None,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._memory = memory
        self._settings = settings
        self._event_bus = event_bus
        self._scheduler = scheduler or ReminderScheduler()
        self._session_context = session_context or SessionContextStore()
        self._health_service = health_service
        self._cancellation_controller = cancellation_controller or CancellationController()
        self._active_requests: set[str] = set()

    @property
    def is_busy(self) -> bool:
        return bool(self._active_requests)

    async def handle_text_async(
        self,
        user_input: str,
        *,
        request_id: int | str | None = None,
    ) -> tuple[str, dict[str, object] | None]:
        text = str(user_input).strip()
        if not text:
            return "I need a request before I can do anything.", {"status": "failed", "steps": []}

        resolved_request_id = str(request_id or self._cancellation_controller.active_request_id or "").strip()
        if not resolved_request_id:
            token = self._cancellation_controller.begin(request_id)
            resolved_request_id = token.request_id
        elif self._cancellation_controller.token_for(resolved_request_id) is None:
            self._cancellation_controller.begin(resolved_request_id)

        self._active_requests.add(resolved_request_id)
        self._publish("security.user_input", {"request_id": resolved_request_id, "text": text})
        self._publish_status(resolved_request_id, "thinking")
        try:
            memory_context = self._memory.build_context(text).as_payload()
            memory_context.update(self._session_context.snapshot().as_payload())
            plan = await self._planner.build_plan_async(
                text,
                memory=memory_context,
                conversation=self._memory.recent_conversation(limit=6),
                system_state=self._system_state(),
            )
            self._publish(
                "execution.plan_created",
                {
                    "request_id": resolved_request_id,
                    "goal": text,
                    "intent": plan.intent,
                    "step_count": len(plan.steps),
                    "steps": [
                        {
                            "step_id": step.step_id,
                            "action": step.action,
                            "target": step.target,
                            "depends_on": list(step.depends_on),
                        }
                        for step in plan.steps
                    ],
                },
            )

            if plan.clarification_question or not plan.steps:
                response = plan.clarification_question or plan.fallback_response or "I could not build a reliable plan."
                self._memory.add_interaction(text, response)
                snapshot = {
                    "status": "completed",
                    "user_input": text,
                    "intent": plan.intent,
                    "goal": plan.goal,
                    "steps": [],
                    "response": response,
                }
                self._publish_status(resolved_request_id, "responding")
                self._publish_response_chunks(resolved_request_id, response)
                self._publish("execution.finalized", {"request_id": resolved_request_id, "status": "clarification", "response": response})
                self._publish("security.response_sent", {"request_id": resolved_request_id, "response": response})
                return response, snapshot

            self._publish_status(resolved_request_id, "executing")
            report = await self._executor.execute_plan(
                plan,
                request_id=resolved_request_id,
                goal=text,
                concurrency_mode="auto",
                runtime_state=self,
            )
            snapshot = report.as_snapshot(user_input=text, plan=plan)
            self._session_context.remember_plan(text, plan, results=snapshot["steps"])
            self._memory.add_interaction(text, report.response, metadata={"request_id": resolved_request_id, "success": report.success})
            self._publish_status(resolved_request_id, "responding")
            self._publish_response_chunks(resolved_request_id, report.response)
            self._publish(
                "execution.finalized",
                {
                    "request_id": resolved_request_id,
                    "status": "completed" if report.success else "failed",
                    "response": report.response,
                },
            )
            self._publish("security.response_sent", {"request_id": resolved_request_id, "response": report.response})
            return report.response, snapshot
        except Exception:
            self._publish_status(resolved_request_id, "error")
            raise
        finally:
            self._active_requests.discard(resolved_request_id)

    def finish_request(self, request_id: int | str | None = None) -> None:
        resolved = str(request_id or "").strip()
        if resolved:
            self._active_requests.discard(resolved)
        self._cancellation_controller.finish(request_id)

    def interrupt(self, request_id: int | str | None = None) -> bool:
        cancelled = self._cancellation_controller.cancel(request_id)
        resolved_request_id = str(request_id or "").strip()
        self._publish("execution.cancel_requested", {"request_id": resolved_request_id})
        if resolved_request_id:
            self._publish_status(resolved_request_id, "cancelled")
        return cancelled

    def poll_notifications(self) -> list[str]:
        due = self._scheduler.poll_due()
        messages = [str(item.get("message", "")).strip() for item in due if str(item.get("message", "")).strip()]
        for message in messages:
            self._publish("reminder.due", {"message": message})
        return messages

    def shutdown(self) -> None:
        if self._event_bus is not None and hasattr(self._event_bus, "shutdown"):
            self._event_bus.shutdown()

    def _system_state(self) -> dict[str, Any]:
        return {
            "active_requests": len(self._active_requests),
            "pending_reminders": self._scheduler.pending_count,
            "safe_mode": self._settings.safe_mode,
            "offline_mode": self._settings.offline_mode,
            "simulate_actions": self._settings.simulate_actions,
        }

    def _publish(self, event_name: str, payload: dict[str, Any]) -> None:
        if self._event_bus is not None:
            self._event_bus.publish(event_name, payload)

    def _publish_status(self, request_id: str, state: str) -> None:
        self._publish(
            "runtime.status",
            {
                "type": "status",
                "request_id": request_id,
                "state": state,
            },
        )

    def _publish_response_chunks(self, request_id: str, response: str) -> None:
        chunks = chunk_response_text(response)
        if not chunks:
            return
        for index, chunk in enumerate(chunks):
            self._publish(
                "runtime.response_chunk",
                {
                    "type": "response_chunk",
                    "request_id": request_id,
                    "data": chunk,
                    "final": index == len(chunks) - 1,
                },
            )

def build_application(*args, **kwargs):
    from jarvis.application.bootstrap import build_application as _build_application

    return _build_application(*args, **kwargs)


__all__ = ["JarvisOrchestrator", "build_application"]
