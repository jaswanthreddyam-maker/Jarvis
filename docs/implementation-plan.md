# Implementation Plan

## Source understanding

The two PDFs describe the same platform at different maturity levels:

- The executive summary defines the baseline system: wake-word, STT, planner, developer agent, action engine, memory, TTS, safety rules, and a staged roadmap.
- The V2 upgrade makes the control plane explicit: orchestrator, task state, retries, event bus, scheduler, permission policy, plugin system, and a more production-ready execution model.

Taken together, the architecture strongly suggests that the first implementation step should not be audio-heavy. The highest-risk part is the structured handoff between planning, execution, recovery, permissions, and memory. That is the part now implemented in this repo.

## Implemented in this slice

- `jarvis/application/orchestrator.py`: central controller for `input -> plan -> execute -> respond`
- `jarvis/core/executor.py`: validated tool dispatch with safety checks
- `jarvis/core/planner.py` and `jarvis/core/brain.py`: LLM-driven planning and reflection
- `jarvis/core/memory/`: short-term, long-term, and semantic memory coordination
- `jarvis/application/runtime_support.py`: event bus, reminders, and session context tracking
- `jarvis/api/app.py`: HTTP and websocket server boundary
- `ui/backend_bridge.py` and `ui/workers/backend_worker.py`: separate-process desktop client transport

## Deferred intentionally

- wake-word detection
- real STT / TTS
- richer model-provider coverage and streaming token adapters
- browser automation
- arbitrary code execution
- full plugin marketplace

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
