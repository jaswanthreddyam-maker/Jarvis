# Jarvis Brain Controller

## Brain Module

Primary implementation:
- `jarvis/core/brain.py`
- `jarvis/core/planner.py`
- `jarvis/infrastructure/ai/llm_client.py`

Runtime flow:
1. `intent_extraction`
2. `task_planning`
3. local tool execution
4. `execution_reflection` on failure

The LLM never executes tools directly. It only returns structured control JSON.

## Prompt Templates

System prompt source:
- `jarvis/infrastructure/ai/prompt_templates.py`

Template structure:
```text
You are Jarvis Brain, the reasoning controller for an autonomous desktop assistant.
You are not a chatbot.
You do not execute tools.
You do not produce free-form prose.
Return exactly one JSON object and nothing else.
Only use tools from allowed_tools.
```

User prompt template:
```text
Controller input for stage `{stage_name}`.

Read the JSON payload below and return exactly one JSON object that satisfies the stage contract.

INPUT_JSON:
{request_payload}
```

## Multi-Step Planning Example

Input goal:
```text
open chrome and search python automation
```

Expected planning shape:
```json
{
  "intent": "multi_step_command",
  "tool": "",
  "args": {},
  "confidence": 0.91,
  "clarification_question": null,
  "response": "",
  "steps": [
    {
      "tool": "open_app",
      "target": "chrome",
      "args": {"app_name": "chrome"},
      "description": "Open the chrome application.",
      "confidence": 0.95,
      "depends_on_previous": false,
      "param_bindings": {}
    },
    {
      "tool": "search_web",
      "target": "python automation",
      "args": {"query": "python automation", "browser_app": "chrome"},
      "description": "Search the web for python automation in chrome.",
      "confidence": 0.88,
      "depends_on_previous": true,
      "param_bindings": {"browser_app": "step_1.data.app_name"}
    }
  ],
  "unresolved_segments": []
}
```

## Reflection Loop Example

Failure input:
```json
{
  "failed_step": {
    "action": "open_app",
    "target": "chrome",
    "params": {"app_name": "chrome"}
  },
  "execution_result": {
    "success": false,
    "error": "not_found",
    "message": "Chrome is unavailable."
  }
}
```

Expected reflection shape:
```json
{
  "decision": "replan",
  "confidence": 0.78,
  "message": "Chrome failed to open, but I can still search in the default browser.",
  "question": "Chrome failed to open. Should I search in the default browser instead?",
  "steps": [
    {
      "tool": "search_web",
      "target": "youtube",
      "args": {"query": "youtube"},
      "description": "Search the web for youtube.",
      "confidence": 0.78,
      "depends_on_previous": false,
      "param_bindings": {}
    }
  ],
  "unresolved_segments": []
}
```

## Context Handling

Context is injected into every stage payload as structured JSON:
```json
{
  "session_context": {
    "last_action": "search_youtube",
    "last_target": "python tutorials",
    "last_app": "youtube",
    "last_goal": "search python tutorials",
    "last_params": {"query": "python tutorials"}
  },
  "conversation_context": [
    {"role": "user", "content": "search python tutorials"}
  ],
  "system_state": {
    "workflow_snapshot": {}
  }
}
```

This is built in `jarvis/infrastructure/ai/llm_client.py` and consumed by `jarvis/core/planner.py`.

## Error Handling Flow

1. Invalid or ambiguous request:
   Brain returns `intent: "unknown"` with `clarification_question`.
2. Invalid plan JSON or missing required args:
   Contract validation rejects it and converts it into a clarification response.
3. Execution failure:
   The executor sends `current_plan`, `failed_step`, and `execution_result` back into reflection.
4. Reflection result:
   `retry`, `replan`, `ask_user`, or `abort`.
5. Recovery execution:
   The system may auto-run a safe recovery or queue a confirmation prompt.

## Observability

Structured reasoning/execution logs now capture:
- planning request input
- stage request payloads
- validated LLM outputs
- created plan summary
- step success/failure
- recovery decisions
- final task completion state
