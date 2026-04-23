from __future__ import annotations

from jarvis.application.handlers.shared import raise_if_cancelled
from jarvis.core.tools import ToolRegistry, ToolResult


def register(registry: ToolRegistry) -> None:
    registry.register("save_preference", save_preference, "Persist a user preference into long-term memory.", required_params=("key", "value"))
    registry.register("remember_fact", remember_fact, "Persist a fact into memory.", required_params=("content",))
    registry.register("recall_memory", recall_memory, "Recall stored facts.")


def save_preference(params: dict[str, object], context) -> ToolResult:
    key = str(params["key"]).strip()
    value = str(params["value"]).strip()
    raise_if_cancelled(context, "Cancelled updating preferences.")
    stored = context.memory.save_preference(key, value, source="llm")
    if stored is None:
        return ToolResult(
            success=False,
            message="I did not store that preference because it looks sensitive or unsafe to persist.",
            error="memory_policy_blocked",
            data={"key": key},
        )
    return ToolResult(
        success=True,
        message=f"I'll remember your preference for {key.replace('_', ' ')}: {value}",
        data={"key": stored["key"], "value": stored["value"]},
    )


def remember_fact(params: dict[str, object], context) -> ToolResult:
    content = str(params["content"]).strip()
    namespace = str(params.get("namespace", "system"))
    category = str(params.get("category", "fact"))
    raise_if_cancelled(context, "Cancelled updating memory.")
    stored = bool(context.memory.remember(namespace=namespace, content=content, category=category))
    if not stored:
        return ToolResult(
            success=False,
            message="I did not store that because it looks sensitive or unsafe to persist.",
            error="memory_policy_blocked",
            data={"namespace": namespace, "category": category},
        )
    return ToolResult(
        success=True,
        message=f"I'll remember that: {content}",
        data={"namespace": namespace, "category": category},
    )


def recall_memory(params: dict[str, object], context) -> ToolResult:
    query = str(params.get("query", "")).strip()
    namespace = str(params.get("namespace", "system"))
    raise_if_cancelled(context, "Cancelled the memory lookup.")
    records = context.memory.recall(query=query, namespace=namespace, limit=5)
    preference_matches = context.memory.relevant_preferences(query, limit=3) if hasattr(context.memory, "relevant_preferences") else {}
    semantic_matches = context.memory.retrieve_similar(query, limit=3) if hasattr(context.memory, "retrieve_similar") else []
    if not records and not preference_matches and not semantic_matches:
        return ToolResult(success=True, message="I could not find anything relevant in memory yet.", data={"matches": []})

    lines: list[str] = []
    if preference_matches:
        lines.extend(f"preference {key}: {value}" for key, value in preference_matches.items())
    lines.extend(record["content"] for record in records)
    lines.extend(match["text"] for match in semantic_matches if match["text"] not in lines)
    return ToolResult(
        success=True,
        message="Here is what I found in memory:\n- " + "\n- ".join(lines),
        data={
            "matches": records,
            "preferences": preference_matches,
            "semantic_matches": semantic_matches,
        },
    )
