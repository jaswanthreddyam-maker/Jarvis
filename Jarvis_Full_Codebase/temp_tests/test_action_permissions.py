from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from assistant.action_engine import ActionEngine
from assistant.actions.registry import ActionRegistry
from assistant.contracts import StepState
from assistant.event_bus import EventBus
from assistant.memory.memory import MemoryManager
from assistant.permissions import PermissionManager
from assistant.plugins.system_plugin import SystemPlugin
from assistant.scheduler import Scheduler
from assistant.settings import load_settings


class ActionPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_simulate = os.environ.get("JARVIS_SIMULATE_ACTIONS")
        self._base_temp_dir = Path("tests/.tmp").resolve()
        self._base_temp_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._previous_simulate is None:
            os.environ.pop("JARVIS_SIMULATE_ACTIONS", None)
        else:
            os.environ["JARVIS_SIMULATE_ACTIONS"] = self._previous_simulate

    def _build_engine(self, *, safe_mode: bool, simulate_actions: bool = False) -> ActionEngine:
        db_path = self._base_temp_dir / f"{self._testMethodName}.db"
        db_path.unlink(missing_ok=True)
        settings = load_settings(memory_db_path=db_path)
        settings.safe_mode = safe_mode
        settings.simulate_actions = simulate_actions

        event_bus = EventBus()
        self.addCleanup(event_bus.shutdown)
        self.addCleanup(lambda: db_path.unlink(missing_ok=True))

        registry = ActionRegistry()
        SystemPlugin().register(registry)
        permissions = PermissionManager(settings.permissions_path)
        permissions.set_confirmation_callback(lambda *_: True)

        return ActionEngine(
            registry=registry,
            permissions=permissions,
            event_bus=event_bus,
            memory=MemoryManager(settings.memory_db_path, settings.db_schema_path),
            scheduler=Scheduler(event_bus),
            settings=settings,
        )

    def test_simulation_mode_simulates_open_url(self) -> None:
        engine = self._build_engine(safe_mode=False, simulate_actions=True)
        step = StepState(
            step_id="open-youtube",
            plan_step_id=1,
            action="open_url",
            target="youtube",
            params={"url": "https://www.youtube.com"},
            description="Open youtube in the browser.",
        )

        with patch("assistant.tools.web_control.webbrowser.open", return_value=True) as mock_open:
            result = engine.execute(step)

        self.assertTrue(result.success)
        self.assertTrue(result.data["simulated"])
        self.assertEqual(result.data["execution_state"], "simulated")
        mock_open.assert_not_called()

    def test_simulation_mode_simulates_open_app(self) -> None:
        engine = self._build_engine(safe_mode=False, simulate_actions=True)
        step = StepState(
            step_id="open-notepad",
            plan_step_id=1,
            action="open_app",
            target="notepad",
            params={"app_name": "notepad"},
            description="Open the notepad application.",
        )

        with patch("assistant.tools.app_control.subprocess.Popen") as mock_popen:
            result = engine.execute(step)

        self.assertTrue(result.success)
        self.assertTrue(result.data["simulated"])
        self.assertEqual(result.data["execution_state"], "simulated")
        mock_popen.assert_not_called()

    def test_simulation_mode_simulates_read_file(self) -> None:
        engine = self._build_engine(safe_mode=False, simulate_actions=True)
        relative_name = "tests/.tmp/permission_read.txt"
        workspace_file = Path(relative_name).resolve()
        workspace_file.write_text("jarvis", encoding="utf-8")
        self.addCleanup(lambda: workspace_file.unlink(missing_ok=True))
        step = StepState(
            step_id="read-file",
            plan_step_id=1,
            action="read_file",
            target=relative_name,
            params={"name": relative_name},
            description="Read the file permission_read.txt.",
        )

        result = engine.execute(step)

        self.assertTrue(result.success)
        self.assertTrue(result.data["simulated"])
        self.assertEqual(result.data["execution_state"], "simulated")
        self.assertNotIn("jarvis", result.message.lower())

    def test_safe_mode_blocks_delete_file(self) -> None:
        engine = self._build_engine(safe_mode=True)
        step = StepState(
            step_id="delete-file",
            plan_step_id=1,
            action="delete_file",
            target="notes.txt",
            params={"name": "notes.txt"},
            description="Delete the file notes.txt.",
            risk_level="high",
        )

        result = engine.execute(step)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "permission_blocked")
        self.assertEqual(result.data["execution_state"], "blocked")
        self.assertIn("safe mode blocks restricted action", result.message.lower())

    def test_safe_mode_blocks_restricted_install(self) -> None:
        engine = self._build_engine(safe_mode=True)
        step = StepState(
            step_id="install-python",
            plan_step_id=1,
            action="install_app",
            target="python",
            params={"app_name": "python"},
            description="Install python.",
            risk_level="high",
        )

        with patch.object(SystemPlugin, "_run_winget") as mock_winget:
            result = engine.execute(step)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "permission_blocked")
        self.assertIn("[BLOCKED]", result.message)
        self.assertEqual(result.data["execution_state"], "blocked")
        mock_winget.assert_not_called()

    def test_explicit_simulation_still_supported(self) -> None:
        engine = self._build_engine(safe_mode=False)
        step = StepState(
            step_id="simulate-search",
            plan_step_id=1,
            action="search_web",
            target="jarvis automation",
            params={"query": "jarvis automation", "simulate": True},
            description="Search the web for jarvis automation.",
        )

        with patch("assistant.tools.web_control.webbrowser.open") as mock_open:
            result = engine.execute(step)

        self.assertTrue(result.success)
        self.assertTrue(result.data["simulated"])
        self.assertEqual(result.data["execution_state"], "simulated")
        self.assertIn("Prepared search", result.message)
        mock_open.assert_not_called()

    def test_focus_app_not_running_returns_not_found_status(self) -> None:
        engine = self._build_engine(safe_mode=False)
        step = StepState(
            step_id="focus-missing-app",
            plan_step_id=1,
            action="focus_app",
            target="chrome",
            params={"app_name": "chrome"},
            description="Focus the chrome application.",
        )

        with patch("assistant.tools.window_control._enumerate_windows", return_value=[]):
            result = engine.execute(step)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.data["status"], "not_found")
        self.assertEqual(result.message, "Chrome is not running.")

    def test_action_engine_publishes_safety_feedback(self) -> None:
        db_path = self._base_temp_dir / f"{self._testMethodName}.db"
        db_path.unlink(missing_ok=True)
        settings = load_settings(memory_db_path=db_path)
        settings.safe_mode = False
        settings.simulate_actions = False

        event_bus = EventBus()
        self.addCleanup(event_bus.shutdown)
        self.addCleanup(lambda: db_path.unlink(missing_ok=True))
        safety_events: list[dict[str, object]] = []
        event_bus.subscribe("safety.status", safety_events.append)

        registry = ActionRegistry()
        SystemPlugin().register(registry)
        permissions = PermissionManager(settings.permissions_path)
        permissions.set_confirmation_callback(lambda *_: None)
        engine = ActionEngine(
            registry=registry,
            permissions=permissions,
            event_bus=event_bus,
            memory=MemoryManager(settings.memory_db_path, settings.db_schema_path),
            scheduler=Scheduler(event_bus),
            settings=settings,
        )

        step = StepState(
            step_id="delete-file",
            plan_step_id=1,
            action="delete_file",
            target="notes.txt",
            params={"name": "notes.txt"},
            description="Delete the file notes.txt.",
            risk_level="high",
        )

        result = engine.execute(step)

        self.assertFalse(result.success)
        self.assertTrue(safety_events)
        self.assertEqual(safety_events[-1]["safety_level"], "CONFIRMATION REQUIRED")


if __name__ == "__main__":
    unittest.main()
