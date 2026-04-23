from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import uuid4

from config.settings import Settings, load_settings
from jarvis.application.controller import JarvisApplication
from jarvis.runtime.background_tasks import NotificationPump
from jarvis.runtime.decision_engine import DecisionEngine
from jarvis.runtime.execution_engine import ExecutionEngine
from jarvis.runtime.input_handler import TextInputHandler, VoiceInputHandler
from jarvis.runtime.output_handler import OutputHandler
from jarvis.runtime.task_manager import RuntimeTaskManager


@dataclass(slots=True)
class RuntimeOptions:
    debug: bool = False
    voice: bool = False
    test_mode: bool = False


class RuntimeController:
    def __init__(
        self,
        *,
        application: JarvisApplication,
        logger: logging.Logger,
        decision_engine: DecisionEngine | None = None,
        execution_engine: ExecutionEngine | None = None,
        task_manager: RuntimeTaskManager | None = None,
        notification_pump: NotificationPump | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._application = application
        self._logger = logger
        self._settings = settings or load_settings()
        self._decision_engine = decision_engine or DecisionEngine()
        self._execution_engine = execution_engine or ExecutionEngine(
            application,
            timeout_seconds=self._settings.api.request_timeout_seconds,
            logger=logger,
        )
        self._task_manager = task_manager or RuntimeTaskManager(
            max_concurrent_requests=self._settings.resources.max_concurrent_requests,
        )
        self._notification_pump = notification_pump or NotificationPump(application)
        self._session_loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    async def run(self, options: RuntimeOptions) -> None:
        output = OutputHandler()
        output.print_banner()
        try:
            if options.test_mode:
                await self._run_test_mode(output)
                return
            if options.voice:
                await self._run_voice_mode(output)
                return
            await self._run_text_mode(output)
        finally:
            self._shutdown_application()

    async def _run_test_mode(self, output: OutputHandler) -> None:
        response = await self._execution_engine.execute(
            "search the web for 'python automation' and write the results to a file named 'demo_search.txt'",
            request_id=self._new_request_id(),
        )
        await output.respond_async(response)
        output.stop()

    async def _run_voice_mode(self, output: OutputHandler) -> None:
        input_handler = VoiceInputHandler(tts_backend=output.backend)
        output.set_interrupt_callback(self._on_voice_output_finished)
        input_handler.start()
        await output.announce_ready_async(voice_mode=True)
        try:
            await self._run_session(
                input_handler,
                output,
                speak_farewell=True,
            )
        except KeyboardInterrupt:
            self._logger.info("Keyboard interrupt received")
        finally:
            input_handler.stop()
            output.stop()
            self._logger.info("Voice loop terminated")

    async def _run_text_mode(self, output: OutputHandler) -> None:
        await output.announce_ready_async(voice_mode=False)
        try:
            await self._run_session(
                TextInputHandler(),
                output,
                speak_farewell=False,
            )
        except KeyboardInterrupt:
            self._logger.info("Keyboard interrupt received")
        finally:
            output.stop()
            self._logger.info("Text loop terminated")

    async def _run_session(
        self,
        input_handler,
        output: OutputHandler,
        *,
        speak_farewell: bool,
    ) -> None:
        self._session_loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        input_queue: asyncio.Queue[object | None] = asyncio.Queue(
            maxsize=self._settings.resources.max_pending_requests,
        )
        output_queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        listen_task = asyncio.create_task(
            self._listen_loop(
                input_handler,
                output,
                input_queue=input_queue,
                output_queue=output_queue,
                speak_farewell=speak_farewell,
            ),
            name="jarvis.listen_loop",
        )
        dispatch_task = asyncio.create_task(
            self._dispatch_loop(input_queue=input_queue, output_queue=output_queue),
            name="jarvis.dispatch_loop",
        )
        output_task = asyncio.create_task(
            self._output_loop(output=output, output_queue=output_queue),
            name="jarvis.output_loop",
        )
        background_task = asyncio.create_task(
            self._notification_pump.run(output_queue, self._stop_event),
            name="jarvis.background_notifications",
        )
        try:
            await listen_task
            await dispatch_task
            await self._task_manager.wait_for_idle()
        finally:
            if not listen_task.done():
                listen_task.cancel()
            self._signal_stop(input_queue)
            if self._stop_event is not None:
                self._stop_event.set()
            self._task_manager.cancel_all(cancel_callback=self._application.cancel_active)
            await self._task_manager.wait_for_idle()
            await asyncio.gather(dispatch_task, background_task, return_exceptions=True)
            await output_queue.put(None)
            await asyncio.gather(output_task, return_exceptions=True)
            self._session_loop = None
            self._stop_event = None

    async def _listen_loop(
        self,
        input_handler,
        output: OutputHandler,
        *,
        input_queue: asyncio.Queue[object | None],
        output_queue: asyncio.Queue[tuple[str, str] | None],
        speak_farewell: bool,
    ) -> None:
        loop = asyncio.get_running_loop()
        while self._stop_event is not None and not self._stop_event.is_set():
            command = await input_handler.listen(loop)
            if command is None:
                if self._stop_event is not None:
                    self._stop_event.set()
                await input_queue.put(None)
                break
            decision = self._decision_engine.decide(command)
            if decision.kind == "ignore":
                continue
            if decision.interrupted:
                self._cancel_latest_request()
            output.show_input(decision)
            await input_queue.put(decision)

    async def _dispatch_loop(
        self,
        *,
        input_queue: asyncio.Queue[object | None],
        output_queue: asyncio.Queue[tuple[str, str] | None],
    ) -> None:
        while True:
            decision = await input_queue.get()
            if decision is None:
                return
            await self._task_manager.acquire_slot()
            request_id = self._new_request_id()
            try:
                task = asyncio.create_task(
                    self._handle_request(
                        decision.text,
                        request_id=request_id,
                        output_queue=output_queue,
                    ),
                    name=f"jarvis.request.{request_id}",
                )
                self._task_manager.register(request_id, task)
            except Exception:
                self._task_manager.release_slot()
                raise

    async def _handle_request(
        self,
        text: str,
        *,
        request_id: str,
        output_queue: asyncio.Queue[tuple[str, str] | None],
    ) -> None:
        try:
            response = await self._execution_engine.execute(
                text,
                request_id=request_id,
            )
            if response:
                await output_queue.put(("response", response))
        except asyncio.CancelledError:
            self._application.cancel_active(request_id)
            raise
        except Exception as exc:
            self._logger.exception("Request %s failed", request_id, exc_info=exc)
            await output_queue.put(
                (
                    "response",
                    "That request failed, but Jarvis is still running and ready for the next one.",
                )
            )
        finally:
            self._task_manager.complete(request_id)

    async def _output_loop(
        self,
        *,
        output: OutputHandler,
        output_queue: asyncio.Queue[tuple[str, str] | None],
    ) -> None:
        while True:
            item = await output_queue.get()
            if item is None:
                return
            kind, text = item
            if kind == "farewell":
                await output.say_farewell_async(text)
                continue
            if kind == "notification":
                await output.respond_async(f"Reminder: {text}")
                continue
            await output.respond_async(text)

    def _on_voice_output_finished(self, interrupted: bool) -> None:
        if not interrupted:
            return
        if self._session_loop is None or not self._session_loop.is_running():
            self._cancel_latest_request()
            return
        self._session_loop.call_soon_threadsafe(self._cancel_latest_request)

    def _cancel_latest_request(self) -> None:
        cancelled = self._task_manager.cancel(cancel_callback=self._application.cancel_active)
        if cancelled:
            self._logger.info("Cancelled active request(s): %s", ", ".join(cancelled))

    def _shutdown_application(self) -> None:
        if hasattr(self._application, "shutdown"):
            try:
                self._application.shutdown()
            except Exception:
                self._logger.exception("Application shutdown failed.")

    @staticmethod
    def _signal_stop(queue: asyncio.Queue[object | None]) -> None:
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    @staticmethod
    def _new_request_id() -> str:
        return uuid4().hex
