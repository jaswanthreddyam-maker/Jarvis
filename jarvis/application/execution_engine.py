from __future__ import annotations

from jarvis.application.handlers import files, memory, runtime, system, web
from jarvis.core.tools import ToolRegistry


def register_tool_handlers(registry: ToolRegistry) -> None:
    web.register(registry)
    system.register(registry)
    files.register(registry)
    memory.register(registry)
    runtime.register(registry)
