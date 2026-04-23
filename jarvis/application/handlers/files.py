from __future__ import annotations

from jarvis.application.handlers.shared import raise_if_cancelled, should_simulate, simulated_result, tool_result
from jarvis.core.tools import ToolRegistry, ToolResult
from jarvis.infrastructure.system_control import file_ops as file_tools


def register(registry: ToolRegistry) -> None:
    registry.register("create_file", create_file, "Create a text file.", required_params=("name",))
    registry.register("overwrite_file", overwrite_file, "Overwrite a text file.", required_params=("name", "content"))
    registry.register("read_file", read_file, "Read a text file.", required_params=("name",))
    registry.register("delete_file", delete_file, "Delete a file.", required_params=("name",))


def create_file(params: dict[str, object], context) -> ToolResult:
    name = str(params["name"]).strip()
    content = str(params.get("content", ""))
    raise_if_cancelled(context, f"Cancelled creating {name}.")
    if should_simulate(params, context):
        return simulated_result("create_file", name, path=name, content=content)
    return tool_result(
        "create_file",
        file_tools.create_file(name=name, content=content, project_root=context.settings.project_root),
        target=name,
    )


def overwrite_file(params: dict[str, object], context) -> ToolResult:
    name = str(params["name"]).strip()
    content = str(params["content"])
    raise_if_cancelled(context, f"Cancelled overwriting {name}.")
    if should_simulate(params, context):
        return simulated_result("overwrite_file", name, path=name, content=content)
    return tool_result(
        "overwrite_file",
        file_tools.overwrite_file(name=name, content=content, project_root=context.settings.project_root),
        target=name,
    )


def read_file(params: dict[str, object], context) -> ToolResult:
    name = str(params["name"]).strip()
    raise_if_cancelled(context, f"Cancelled reading {name}.")
    if should_simulate(params, context):
        return simulated_result("read_file", name, path=name)
    result = file_tools.read_file(name=name, project_root=context.settings.project_root)
    if result.success and "content" in result.data:
        result.message = f"{result.message}\n{result.data['content']}"
    return tool_result("read_file", result, target=name)


def delete_file(params: dict[str, object], context) -> ToolResult:
    name = str(params["name"]).strip()
    raise_if_cancelled(context, f"Cancelled deleting {name}.")
    if should_simulate(params, context):
        return simulated_result("delete_file", name, path=name)
    return tool_result(
        "delete_file",
        file_tools.delete_file(name=name, project_root=context.settings.project_root),
        target=name,
    )
