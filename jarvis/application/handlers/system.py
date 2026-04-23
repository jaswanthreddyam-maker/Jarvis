from __future__ import annotations

from jarvis.application.handlers.shared import raise_if_cancelled, should_simulate, simulated_result, tool_result, truthy
from jarvis.core.tools import ToolRegistry, ToolResult
from jarvis.infrastructure.system_control import open_app as system_tools


def register(registry: ToolRegistry) -> None:
    registry.register("emergency_stop", emergency_stop, "Cancel all running tasks immediately.")
    registry.register("system_action", system_action, "Execute a system power action.", required_params=("action_type",))
    registry.register("open_app", open_app, "Launch a desktop application.", required_params=("app_name",))
    registry.register("focus_app", focus_app, "Bring an application to the foreground.", required_params=("app_name",))
    registry.register("close_app", close_app, "Close an application.", required_params=("app_name",))
    registry.register("set_volume", set_volume, "Adjust the system volume.", required_params=("value",))
    registry.register("open_explorer", open_explorer, "Open a folder in File Explorer.")
    registry.register("get_clipboard", get_clipboard, "Read clipboard text.")
    registry.register("set_clipboard", set_clipboard, "Replace clipboard text.", required_params=("text",))
    registry.register("minimize_window", minimize_window, "Minimize the active window.")
    registry.register("switch_window", switch_window, "Switch to a window by title.", required_params=("title",))
    registry.register("close_active_window", close_active_window, "Close the active window.")
    registry.register("install_app", install_app, "Install an application using winget.", required_params=("app_name",))


def system_action(params: dict[str, object], context) -> ToolResult:
    action_type = str(params["action_type"]).strip().lower()
    raise_if_cancelled(context, f"Cancelled system action {action_type}.")
    if should_simulate(params, context):
        return simulated_result("system_action", action_type, action_type=action_type)
    return tool_result(
        "system_action",
        system_tools.system_action(action_type, confirmed=truthy(params.get("_confirmed_system_action", False))),
        target=action_type,
    )


def emergency_stop(params: dict[str, object], context) -> ToolResult:
    del params
    guard = getattr(context, "safety_guard", None)
    if guard is None or not hasattr(guard, "trigger_emergency_stop"):
        return ToolResult(
            success=False,
            message="Safety guard is unavailable, so I could not trigger an emergency stop.",
            error="guard_unavailable",
        )
    result = guard.trigger_emergency_stop(reason="User requested emergency stop.")
    cancelled_count = int(result.get("cancelled_count", 0) or 0)
    return ToolResult(
        success=True,
        message=f"Emergency stop executed. Cancelled {cancelled_count} running request(s).",
        data=result,
    )


def open_app(params: dict[str, object], context) -> ToolResult:
    app_name = str(params["app_name"]).strip().lower()
    raise_if_cancelled(context, f"Cancelled opening {app_name}.")
    if should_simulate(params, context):
        return simulated_result("open_app", app_name, app_name=app_name)
    return tool_result("open_app", system_tools.open_app(app_name), target=app_name)


def focus_app(params: dict[str, object], context) -> ToolResult:
    app_name = str(params["app_name"]).strip()
    raise_if_cancelled(context, f"Cancelled focusing {app_name}.")
    if should_simulate(params, context):
        return simulated_result("focus_app", app_name, app_name=app_name)
    return tool_result("focus_app", system_tools.focus_app(app_name), target=app_name, app_name=app_name)


def close_app(params: dict[str, object], context) -> ToolResult:
    app_name = str(params["app_name"]).strip()
    raise_if_cancelled(context, f"Cancelled closing {app_name}.")
    if should_simulate(params, context):
        return simulated_result("close_app", app_name, app_name=app_name)
    return tool_result("close_app", system_tools.close_app(app_name), target=app_name, app_name=app_name)


def set_volume(params: dict[str, object], context) -> ToolResult:
    value = str(params["value"]).strip()
    raise_if_cancelled(context, "Cancelled the volume change.")
    if should_simulate(params, context):
        return simulated_result("set_volume", value, value=value)
    return tool_result("set_volume", system_tools.set_volume(value), target=value)


def open_explorer(params: dict[str, object], context) -> ToolResult:
    path = str(params.get("path", "project")).strip() or "project"
    raise_if_cancelled(context, f"Cancelled opening {path}.")
    if should_simulate(params, context):
        return simulated_result("open_explorer", path, path=path)
    return tool_result(
        "open_explorer",
        system_tools.open_explorer(path, project_root=context.settings.project_root),
        target=path,
    )


def get_clipboard(params: dict[str, object], context) -> ToolResult:
    del params
    raise_if_cancelled(context, "Cancelled reading the clipboard.")
    if should_simulate({}, context):
        return simulated_result("get_clipboard", "clipboard", text="")
    result = system_tools.get_clipboard()
    if result.success:
        result.message = f"{result.message}\n{result.data.get('text', '')}"
    return tool_result("get_clipboard", result, target="clipboard")


def set_clipboard(params: dict[str, object], context) -> ToolResult:
    text = str(params["text"])
    raise_if_cancelled(context, "Cancelled updating the clipboard.")
    if should_simulate(params, context):
        return simulated_result("set_clipboard", text, text=text)
    return tool_result("set_clipboard", system_tools.set_clipboard(text), target=text)


def minimize_window(params: dict[str, object], context) -> ToolResult:
    del params
    raise_if_cancelled(context, "Cancelled minimizing the active window.")
    if should_simulate({}, context):
        return simulated_result("minimize_window", "active window")
    return tool_result("minimize_window", system_tools.minimize_window(), target="active window")


def switch_window(params: dict[str, object], context) -> ToolResult:
    title = str(params["title"]).strip()
    raise_if_cancelled(context, f"Cancelled switching to {title}.")
    if should_simulate(params, context):
        return simulated_result("switch_window", title, title=title)
    return tool_result("switch_window", system_tools.switch_window(title), target=title)


def close_active_window(params: dict[str, object], context) -> ToolResult:
    del params
    raise_if_cancelled(context, "Cancelled closing the active window.")
    if should_simulate({}, context):
        return simulated_result("close_active_window", "active window")
    return tool_result("close_active_window", system_tools.close_active_window(), target="active window")


def install_app(params: dict[str, object], context) -> ToolResult:
    app_name = str(params["app_name"]).strip()
    raise_if_cancelled(context, f"Cancelled installing {app_name}.")
    if should_simulate(params, context):
        return simulated_result("install_app", app_name, app_name=app_name)
    return tool_result(
        "install_app",
        system_tools.install_app(app_name, token=getattr(context, "cancellation_token", None)),
        target=app_name,
    )
