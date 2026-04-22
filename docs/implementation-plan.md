# Implementation Plan

## Source understanding

The two PDFs describe the same platform at different maturity levels:

- The executive summary defines the baseline system: wake-word, STT, planner, developer agent, action engine, memory, TTS, safety rules, and a staged roadmap.
- The V2 upgrade makes the control plane explicit: orchestrator, task state, retries, event bus, scheduler, permission policy, plugin system, and a more production-ready execution model.

Taken together, the architecture strongly suggests that the first implementation step should not be audio-heavy. The highest-risk part is the structured handoff between planning, execution, recovery, permissions, and memory. That is the part now implemented in this repo.

## Implemented in this slice

- `assistant/orchestrator.py`: central controller for `input -> plan -> execute -> respond`
- `assistant/state_manager.py`: task and step lifecycle tracking
- `assistant/error_handler.py`: retry policy loading
- `assistant/action_engine.py`: safe dispatch with policy checks
- `assistant/actions/` and `assistant/plugins/system_plugin.py`: allowlisted starter tools
- `assistant/memory/`: SQLite persistence plus session context
- `assistant/event_bus.py` and `assistant/scheduler.py`: event-driven reminder pipeline
- `assistant/agents/claude_agent.py`: rule-based planner standing in for future Claude integration
- `assistant/main.py`: text CLI for running the system today

## Deferred intentionally

- wake-word detection
- real STT / TTS
- real Claude / Codex / Ollama clients
- browser automation
- arbitrary code execution
- full plugin marketplace
- streamed responses

## Why this slice first

This gives us a runnable system that already proves:

- tasks can be planned and executed end to end
- state transitions are visible
- memory survives process restarts
- permission policy blocks unsafe work
- event-driven features can be layered in without reworking the architecture

## Next milestones

1. Replace the rule-based planner with a Claude/Ollama planner adapter that emits the same plan contract.
2. Expand the action layer into real OS/browser tools with stronger confirmation flows.
3. Add wake-word, VAD, STT, and TTS adapters around the existing orchestration core.
4. Add richer schemas, telemetry, and a debug dashboard for inspecting task execution.
