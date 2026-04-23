from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.application.execution_engine import register_tool_handlers
from jarvis.application.runtime_support import EventBus, ReminderScheduler
from jarvis.config.settings import load_settings
from jarvis.core.cancellation import CancellationController
from jarvis.core.context import ExecutionPlan, ExecutionStep
from jarvis.core.executor import ToolExecutor
from jarvis.core.memory import LongTermMemory, MemoryManager, SemanticMemory, ShortTermMemory
from jarvis.core.safety import ExecutionGuard, ExecutionSandbox, SafetyAuditLogger, ToolValidator
from jarvis.core.safety.guard import ActionRateLimiter
from jarvis.core.tools import ToolRegistry, ToolResult
from tests.support_embeddings import FakeEmbeddingProvider


class SafetyLayerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._base_temp_dir = Path("tests/.tmp").resolve()
        self._base_temp_dir.mkdir(parents=True, exist_ok=True)

    def _build_executor(
        self,
        *,
        simulate_actions: bool = False,
        safe_mode: bool = False,
        registry: ToolRegistry | None = None,
        guard: ExecutionGuard | None = None,
        event_bus: EventBus | None = None,
        cancellation_controller: CancellationController | None = None,
    ) -> tuple[ToolExecutor, Path, EventBus, CancellationController]:
        db_path = self._base_temp_dir / f"{self._testMethodName}.db"
        db_path.unlink(missing_ok=True)
        db_path.with_suffix(".long_term.json").unlink(missing_ok=True)
        db_path.with_suffix(".semantic.json").unlink(missing_ok=True)
        settings = load_settings(memory_db_path=db_path)
        settings.simulate_actions = simulate_actions
        settings.safe_mode = safe_mode

        resolved_event_bus = event_bus or EventBus()
        resolved_registry = registry or ToolRegistry()
        if registry is None:
            register_tool_handlers(resolved_registry)
        resolved_cancellation = cancellation_controller or CancellationController()
        executor = ToolExecutor(
            registry=resolved_registry,
            validator=ToolValidator(),
            guard=guard or ExecutionGuard(event_bus=resolved_event_bus, cancellation_controller=resolved_cancellation),
            sandbox=ExecutionSandbox(),
            settings=settings,
            memory=MemoryManager(
                short_term=ShortTermMemory(),
                long_term=LongTermMemory(db_path.with_suffix(".long_term.json")),
                semantic=SemanticMemory(
                    db_path.with_suffix(".semantic.json"),
                    embedding_provider=FakeEmbeddingProvider(),
                ),
            ),
            scheduler=ReminderScheduler(),
            event_bus=resolved_event_bus,
            cancellation_controller=resolved_cancellation,
        )
        self.addCleanup(lambda: db_path.unlink(missing_ok=True))
        self.addCleanup(lambda: db_path.with_suffix(".long_term.json").unlink(missing_ok=True))
        self.addCleanup(lambda: db_path.with_suffix(".semantic.json").unlink(missing_ok=True))
        return executor, db_path, resolved_event_bus, resolved_cancellation

    async def test_raw_shell_tool_is_blocked_even_if_registered(self) -> None:
        registry = ToolRegistry()
        registry.register("run_command", lambda params, context: ToolResult(success=True, message="should not run"), required_params=("command",))
        executor, _, _, cancellation = self._build_executor(registry=registry)
        cancellation.begin("req-1")
        plan = ExecutionPlan(intent="run_command", steps=[ExecutionStep(action="run_command", step_id=1, params={"command": "dir"})])

        report = await executor.execute_plan(plan, request_id="req-1", goal="dir")

        self.assertFalse(report.success)
        self.assertEqual(report.steps[0].error, "validation_blocked")
        self.assertIn("blocked by safety policy", report.steps[0].message.lower())

    async def test_invalid_browser_app_is_rejected(self) -> None:
        executor, _, _, cancellation = self._build_executor()
        cancellation.begin("req-2")
        plan = ExecutionPlan(intent="open_url", steps=[ExecutionStep(action="open_url", step_id=1, params={"url": "https://github.com", "browser_app": "cmd.exe"})])

        with patch("jarvis.infrastructure.web.browser.webbrowser.open") as mock_open:
            report = await executor.execute_plan(plan, request_id="req-2", goal="open github")

        self.assertFalse(report.success)
        self.assertEqual(report.steps[0].error, "validation_blocked")
        mock_open.assert_not_called()

    async def test_guard_runs_before_sandbox_for_dangerous_rate_limited_action(self) -> None:
        guard = ExecutionGuard(rate_limiter=ActionRateLimiter(safe_limit=20, moderate_limit=10, dangerous_limit=0))
        executor, _, _, cancellation = self._build_executor(simulate_actions=True, guard=guard)
        cancellation.begin("req-3")
        plan = ExecutionPlan(intent="delete_file", steps=[ExecutionStep(action="delete_file", step_id=1, params={"name": "notes.txt"})])

        report = await executor.execute_plan(plan, request_id="req-3", goal="delete notes.txt")

        self.assertFalse(report.success)
        self.assertEqual(report.steps[0].error, "rate_limited")

    async def test_action_timeout_cancels_slow_step(self) -> None:
        registry = ToolRegistry()

        async def slow_action(params, context):
            import asyncio

            await asyncio.sleep(float(params.get("seconds", 0.5)))
            return ToolResult(success=True, message="Completed slow action.")

        registry.register("wait_action", slow_action, required_params=("seconds",))
        executor, _, _, cancellation = self._build_executor(
            registry=registry,
            guard=ExecutionGuard(default_timeout_seconds=0.05, moderate_timeout_seconds=0.05, dangerous_timeout_seconds=0.05),
        )
        cancellation.begin("req-4")
        plan = ExecutionPlan(intent="wait_action", steps=[ExecutionStep(action="wait_action", step_id=1, params={"seconds": 0.5})])

        report = await executor.execute_plan(plan, request_id="req-4", goal="wait")

        self.assertFalse(report.success)
        self.assertEqual(report.steps[0].error, "action_timeout")
        self.assertEqual(report.steps[0].data["execution_state"], "cancelled")

    async def test_audit_logger_records_user_input_plan_and_action_events(self) -> None:
        audit_path = self._base_temp_dir / f"{self._testMethodName}.jsonl"
        audit_path.unlink(missing_ok=True)
        event_bus = EventBus()
        logger = SafetyAuditLogger(audit_path)
        logger.attach(event_bus)
        executor, _, event_bus, cancellation = self._build_executor(event_bus=event_bus)
        cancellation.begin("req-5")
        plan = ExecutionPlan(intent="get_time", steps=[ExecutionStep(action="get_time", step_id=1)])

        event_bus.publish("security.user_input", {"request_id": "req-5", "text": "what time is it"})
        event_bus.publish("execution.plan_created", {"request_id": "req-5", "goal": "what time is it"})
        await executor.execute_plan(plan, request_id="req-5", goal="what time is it")

        entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        event_names = {entry["event"] for entry in entries}
        self.assertIn("security.user_input", event_names)
        self.assertIn("execution.plan_created", event_names)
        self.assertIn("action.started", event_names)
        self.assertTrue({"action.completed", "action.failed"} & event_names)

    async def test_emergency_stop_cancels_active_requests(self) -> None:
        cancellation = CancellationController()
        token = cancellation.begin("req-6")
        executor, _, _, _ = self._build_executor(cancellation_controller=cancellation)
        plan = ExecutionPlan(intent="emergency_stop", steps=[ExecutionStep(action="emergency_stop", step_id=1)])

        report = await executor.execute_plan(plan, request_id="req-6", goal="stop")

        self.assertTrue(report.success)
        self.assertTrue(token.is_cancelled())


if __name__ == "__main__":
    unittest.main()
