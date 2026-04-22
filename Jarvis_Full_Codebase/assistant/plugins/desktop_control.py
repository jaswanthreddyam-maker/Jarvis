from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from assistant.memory.memory import MemoryManager
from assistant.plugins.interface import PluginInterface
from .desktop_executor import DesktopExecutor, ExecutionReport

logger = logging.getLogger("Jarvis.DesktopControl")


class DesktopControlPlugin(PluginInterface):
    """
    Desktop control planner interface.

    This is the *planner* half of the desktop automation system:
      - Parses step JSON from the AI agent.
      - Delegates execution to DesktopExecutor (the *executor*).
      - Tracks workflow success/failure in long-term memory.
      - Auto-updates workflows on failure so future runs use the latest version.
      - Provides recall for previously learned workflows.
    """

    def __init__(self) -> None:
        super().__init__()
        app_data_dir = Path(__file__).parent.parent.parent
        self._memory = MemoryManager(
            db_path=app_data_dir / "assistant" / "memory" / "longterm.db",
            schema_path=app_data_dir / "assistant" / "memory" / "db_schema.sql",
        )
        self.executor = DesktopExecutor()

    def activate(self) -> None:
        logger.info("[DesktopControl] Plugin activated. Failsafe enabled.")

    def deactivate(self) -> None:
        self.executor.interrupt()
        logger.info("[DesktopControl] Plugin deactivated. Execution interrupted.")

    def get_tools(self) -> Dict[str, Any]:
        return {
            "get_active_window": self.get_active_window,
            "execute_desktop_workflow": self.execute_desktop_workflow,
            "remember_workflow": self.remember_workflow,
            "recall_workflow": self.recall_workflow,
            "interrupt_execution": self.interrupt_execution,
        }

    # ── Tools exposed to the AI agent ────────────────────────────────
    async def get_active_window(self) -> str:
        """
        Returns the title of the currently active foreground window on Windows.
        If the Jarvis UI overlay has stolen focus, it returns the last known window.
        """
        return self.executor.get_active_window()

    async def execute_desktop_workflow(
        self,
        steps_json: str,
        expected_window: str | None = None,
        require_confirmation: bool = False,
        workflow_name: str = "",
    ) -> str:
        """
        Execute a sequence of desktop actions.

        steps_json must be a JSON array of step objects.
        Valid actions:
            {"action": "type", "text": "hello"}
            {"action": "press", "keys": "ctrl,c"}
            {"action": "click", "button": "left", "clicks": 1}
            {"action": "move_rel", "x": 100, "y": 0, "duration": 0.25}
            {"action": "click_image", "image_name": "icon.png", "confidence": 0.8}
            {"action": "scroll", "amount": -3}
            {"action": "wait", "delay": 1.5}
            {"action": "wait_for_window", "title": "Chrome", "timeout": 10.0}
            {"action": "wait_for_url", "url_fragment": "youtube.com", "timeout": 10.0}

        Each step may include:
            "expected_window"  — abort if the active window doesn't match
            "pre_wait"         — wait condition before the step
            "post_wait"        — wait condition after the step
            "description"      — human-readable label for UI feedback

        Args:
            steps_json: JSON array of step dicts.
            expected_window: Global window to validate before EVERY step.
            require_confirmation: If True, return early and ask for user approval.
            workflow_name: Optional name to auto-track success/failure in memory.
        """
        if require_confirmation:
            return (
                "⚠️ Execution paused. This workflow contains risky actions. "
                "Ask the user for explicit confirmation before proceeding."
            )

        try:
            steps = json.loads(steps_json)
        except Exception as e:
            return f"Invalid JSON in steps_json: {e}"

        report: ExecutionReport = self.executor.execute(steps, expected_window)

        # ── Build feedback log ──
        log_lines = report.log_lines
        for line in log_lines:
            logger.info("[Desktop Executor] %s", line)

        final_log = "\n".join(log_lines)

        # ── Auto-track in workflow memory ──
        if workflow_name:
            await self.remember_workflow(
                workflow_name=workflow_name,
                steps_json=steps_json,
                was_successful=report.success,
            )

        # ── Compose result string ──
        if report.interrupted:
            return f"⛔ Workflow interrupted.\nExecution Log:\n{final_log}"
        if report.window_mismatch:
            return f"🚫 Workflow aborted (window mismatch).\nExecution Log:\n{final_log}"
        if report.success:
            return f"✅ Workflow completed successfully.\nExecution Log:\n{final_log}"
        return f"❌ Workflow failed.\nExecution Log:\n{final_log}"

    async def interrupt_execution(self) -> str:
        """Stop any in-progress desktop workflow cleanly after the current step."""
        self.executor.interrupt()
        return "Interrupt signal sent. The current workflow will stop after the active step."

    async def remember_workflow(
        self,
        workflow_name: str,
        steps_json: str,
        was_successful: bool = True,
    ) -> str:
        """
        Save or update a workflow in long-term memory.

        If *was_successful* is False the workflow is tagged as '[Failing]' so
        future recall can prioritize updating it.
        """
        status = "Verified" if was_successful else "Failing"
        content = f"[{status}] Workflow '{workflow_name}': {steps_json}"

        # Overwrite any prior version of this workflow
        existing = self._memory.recall(
            workflow_name, namespace="desktop_workflows", limit=5
        )
        if existing:
            # Append a revision note so the history is preserved
            revision_note = f"[Revision] Previous version superseded. Status: {status}."
            self._memory.remember(
                "desktop_workflows", revision_note, category="automation"
            )

        self._memory.remember("desktop_workflows", content, category="automation")
        return f"Workflow '{workflow_name}' saved ({status})."

    async def recall_workflow(self, workflow_name: str) -> str:
        """Recall a learned workflow pattern from system memory."""
        results = self._memory.recall(
            workflow_name, namespace="desktop_workflows", limit=3
        )
        if results:
            entries = []
            for r in results:
                entries.append(r["content"])
            return f"Workflow data for '{workflow_name}':\n" + "\n".join(entries)
        return f"No learned workflow found matching '{workflow_name}'."
