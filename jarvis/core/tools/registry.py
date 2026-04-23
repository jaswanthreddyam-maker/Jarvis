from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from jarvis.core.cancellation import CancelledError
from jarvis.core.tools.base import ToolContext, ToolResult


@dataclass(slots=True)
class ToolRegistration:
    name: str
    handler: Callable[[dict[str, Any], ToolContext], ToolResult]
    description: str = ""
    required_params: tuple[str, ...] = field(default_factory=tuple)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolRegistration] = {}

    def register(
        self,
        name: str | None = None,
        handler: Callable[[dict[str, Any], ToolContext], ToolResult] | None = None,
        description: str = "",
        required_params: tuple[str, ...] = (),
    ):
        if handler is None:
            def decorator(func: Callable[[dict[str, Any], ToolContext], ToolResult]):
                tool_name = (name or func.__name__).strip()
                self._tools[tool_name] = ToolRegistration(
                    name=tool_name,
                    handler=func,
                    description=description,
                    required_params=required_params,
                )
                return func

            return decorator

        tool_name = (name or handler.__name__).strip()
        self._tools[tool_name] = ToolRegistration(
            name=tool_name,
            handler=handler,
            description=description,
            required_params=required_params,
        )
        return handler

    def get_tool(self, name: str) -> ToolRegistration | None:
        return self._tools.get(str(name).strip())

    def list_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    async def call_async(self, name: str, params: dict[str, Any], context: ToolContext) -> ToolResult:
        registration = self.get_tool(name)
        if registration is None:
            return ToolResult(
                success=False,
                message=f"Tool '{name}' is not registered.",
                error="tool_not_found",
            )

        missing = [item for item in registration.required_params if item not in params]
        if missing:
            return ToolResult(
                success=False,
                message=f"Missing required parameter(s) for '{registration.name}': {', '.join(missing)}.",
                error="missing_parameter",
            )

        try:
            if inspect.iscoroutinefunction(registration.handler):
                return await registration.handler(params, context)
            return await asyncio.to_thread(registration.handler, params, context)
        except CancelledError:
            raise
        except Exception as exc:
            return ToolResult(
                success=False,
                message=f"Tool '{registration.name}' failed: {exc}",
                error="execution_error",
            )
