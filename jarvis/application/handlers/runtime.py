from __future__ import annotations

from datetime import datetime, timedelta

from jarvis.application.handlers.shared import raise_if_cancelled
from jarvis.core.tools import ToolRegistry, ToolResult


def register(registry: ToolRegistry) -> None:
    registry.register("set_reminder", set_reminder, "Schedule a reminder.", required_params=("message", "delay_seconds"))
    registry.register("health_check", health_check, "Report runtime health.")
    registry.register("report_capabilities", report_capabilities, "Describe the current capabilities.")


def set_reminder(params: dict[str, object], context) -> ToolResult:
    message = str(params["message"]).strip()
    delay_seconds = max(0, int(params["delay_seconds"]))
    raise_if_cancelled(context, f"Cancelled the reminder for {message}.")
    due_at = datetime.now() + timedelta(seconds=delay_seconds)
    context.scheduler.schedule(
        run_at=due_at,
        topic="reminder.due",
        payload={"message": f"Reminder: {message}", "due_at": due_at.isoformat()},
    )
    return ToolResult(
        success=True,
        message=f"Reminder set for {due_at.strftime('%Y-%m-%d %H:%M:%S')}: {message}",
        data={"message": message, "due_at": due_at.isoformat()},
    )


def health_check(params: dict[str, object], context) -> ToolResult:
    del params
    raise_if_cancelled(context, "Cancelled the health check.")
    guard = getattr(context, "safety_guard", None)
    guard_status = guard.status() if guard is not None and hasattr(guard, "status") else {}
    health_service = getattr(context, "health_service", None)
    health_snapshot = health_service.snapshot() if health_service is not None and hasattr(health_service, "snapshot") else {}
    return ToolResult(
        success=True,
        message=(
            "Health OK. "
            f"safe_mode={context.settings.safe_mode}, "
            f"offline_mode={context.settings.offline_mode}, "
            f"simulate_actions={context.settings.simulate_actions}, "
            f"pending_reminders={context.scheduler.pending_count}, "
            f"memory_rss_mb={health_snapshot.get('process', {}).get('memory_rss_mb')}, "
            f"last_emergency_stop={bool(guard_status.get('last_emergency_stop'))}"
        ),
        data={
            "safe_mode": context.settings.safe_mode,
            "offline_mode": context.settings.offline_mode,
            "simulate_actions": context.settings.simulate_actions,
            "pending_reminders": context.scheduler.pending_count,
            "guard": guard_status,
            "health": health_snapshot,
        },
    )


def report_capabilities(params: dict[str, object], context) -> ToolResult:
    del params
    raise_if_cancelled(context, "Cancelled the capabilities report.")
    return ToolResult(
        success=True,
        message=(
            "Current capabilities: open_url, search_web, search_youtube, play_youtube, "
            "open_app, focus_app, close_app, emergency_stop, file operations, system volume, explorer, "
            "clipboard control, window control, reminders, memory, health checks, and runtime observation."
        ),
    )
