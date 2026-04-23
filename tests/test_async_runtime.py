from __future__ import annotations

import asyncio
import logging
import unittest

from jarvis.runtime.background_tasks import NotificationPump
from jarvis.runtime.controller import RuntimeController
from jarvis.runtime.execution_engine import ExecutionEngine as RuntimeExecutionEngine
from jarvis.runtime.input_handler import RuntimeInput
from jarvis.runtime.task_manager import RuntimeTaskManager


class FakeApplication:
    def __init__(
        self,
        *,
        delays: dict[str, float] | None = None,
        notifications: list[str] | None = None,
    ) -> None:
        self._delays = dict(delays or {})
        self._notifications = list(notifications or [])
        self.started: list[str] = []
        self.finished: list[str] = []
        self.cancelled: list[str | None] = []
        self.inflight = 0
        self.max_inflight = 0

    def begin_execution(self, request_id: int | str | None = None) -> None:
        self.started.append("" if request_id is None else str(request_id))

    async def handle_text_async(
        self,
        user_input: str,
        request_id: int | str | None = None,
    ) -> tuple[str, dict[str, object] | None]:
        del request_id
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self._delays.get(user_input, 0.01))
            return f"done: {user_input}", {"status": "completed"}
        finally:
            self.inflight -= 1

    def cancel_active(self, request_id: int | str | None = None) -> bool:
        self.cancelled.append(None if request_id is None else str(request_id))
        return True

    def finish_execution(self, request_id: int | str | None = None) -> None:
        self.finished.append("" if request_id is None else str(request_id))

    def poll_notifications(self) -> list[str]:
        if not self._notifications:
            return []
        return [self._notifications.pop(0)]


class ScriptedInputHandler:
    def __init__(self, items: list[str | RuntimeInput | None]) -> None:
        self._items = list(items)
        self._index = 0

    async def listen(self, loop) -> RuntimeInput | None:
        del loop
        await asyncio.sleep(0)
        item = self._items[self._index]
        self._index += 1
        if item is None:
            return None
        if isinstance(item, RuntimeInput):
            return item
        return RuntimeInput(text=item, source="text")


class RecordingOutput:
    def __init__(self) -> None:
        self.responses: list[str] = []
        self.inputs: list[str] = []
        self.farewells: list[str] = []
        self._callback = None

    @property
    def backend(self):
        return None

    def print_banner(self) -> None:
        pass

    async def announce_ready_async(self, *, voice_mode: bool) -> None:
        del voice_mode

    def set_interrupt_callback(self, callback) -> None:
        self._callback = callback

    def show_input(self, decision) -> None:
        self.inputs.append(decision.text)

    async def respond_async(self, response: str) -> None:
        self.responses.append(response)

    async def say_farewell_async(self, text: str = "Shutting down. Goodbye.") -> None:
        self.farewells.append(text)

    def stop(self) -> None:
        pass


class AsyncRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_controller_dispatches_commands_concurrently(self) -> None:
        application = FakeApplication(delays={"alpha": 0.05, "beta": 0.05})
        controller = RuntimeController(
            application=application,
            logger=logging.getLogger("test.async.runtime.concurrent"),
        )
        output = RecordingOutput()

        await controller._run_session(
            ScriptedInputHandler(["alpha", "beta", None]),
            output,
            speak_farewell=False,
        )

        self.assertGreaterEqual(application.max_inflight, 2)
        self.assertCountEqual(output.responses, ["done: alpha", "done: beta"])
        self.assertEqual(len(application.started), 2)
        self.assertEqual(len(application.finished), 2)

    async def test_runtime_controller_respects_configured_concurrency_limit(self) -> None:
        application = FakeApplication(delays={"alpha": 0.05, "beta": 0.05})
        controller = RuntimeController(
            application=application,
            logger=logging.getLogger("test.async.runtime.serial"),
            task_manager=RuntimeTaskManager(max_concurrent_requests=1),
        )
        output = RecordingOutput()

        await controller._run_session(
            ScriptedInputHandler(["alpha", "beta", None]),
            output,
            speak_farewell=False,
        )

        self.assertEqual(application.max_inflight, 1)
        self.assertCountEqual(output.responses, ["done: alpha", "done: beta"])

    async def test_runtime_execution_engine_times_out_and_cancels_request(self) -> None:
        application = FakeApplication(delays={"slow": 0.2})
        engine = RuntimeExecutionEngine(
            application,
            timeout_seconds=0.01,
            logger=logging.getLogger("test.async.runtime.timeout"),
        )

        response = await engine.execute("slow", request_id="timeout-request")

        self.assertIn("timed out", response.lower())
        self.assertIn("timeout-request", application.cancelled)
        self.assertIn("timeout-request", application.finished)

    async def test_background_notifications_are_pumped_during_active_work(self) -> None:
        application = FakeApplication(
            delays={"alpha": 0.05},
            notifications=["stretch"],
        )
        controller = RuntimeController(
            application=application,
            logger=logging.getLogger("test.async.runtime.notifications"),
            notification_pump=NotificationPump(application, poll_interval=0.01),
        )
        output = RecordingOutput()

        await controller._run_session(
            ScriptedInputHandler(["alpha", None]),
            output,
            speak_farewell=False,
        )

        self.assertIn("Reminder: stretch", output.responses)
        self.assertIn("done: alpha", output.responses)


if __name__ == "__main__":
    unittest.main()
