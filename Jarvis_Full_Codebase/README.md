# Jarvis

This repo bootstraps the first working slice of the architecture described in the supplied PDFs. The current milestone is a text-first control plane: it plans a request, validates action permissions, executes them through an orchestrator, tracks task state, stores memory in SQLite, and exposes a simple CLI.

Voice I/O, real Claude/Codex calls, streaming, and full OS/browser automation are intentionally stubbed for the next phase. The goal of this slice is to make the execution backbone real before we attach wake-word, STT, TTS, and cloud/local model integrations.

## What works now

- Central orchestrator with task-state tracking
- Retry/policy handling and safe action allowlist
- SQLite-backed long-term memory plus in-session context
- Event bus plus reminder scheduling
- Text CLI for one-shot commands or interactive use
- Real execution for safe URL, web search, and app launch actions

## Quick start

```powershell
python -m assistant.main "open youtube" --json
python -m assistant.main "remember that I like dark mode"
python -m assistant.main "what do you remember about dark mode"
python -m assistant.main
```

## Test

```powershell
python -m unittest discover -s tests
```

## Current examples

- `open youtube`
- `search for local llm setup`
- `what time is it`
- `remember that my favorite editor is vscode`
- `what do you remember about editor`
- `remind me to stretch in 0 seconds`
- `health`

## Layout

- `assistant/`: runtime package
- `assistant/config/`: model/runtime config and env template
- `assistant/prompts/`: prompt contracts for future LLM integrations
- `assistant/schemas/`: JSON schemas for intent, plans, and tool calls
- `docs/source/`: extracted text from the two architecture PDFs
- `docs/implementation-plan.md`: implementation interpretation and MVP scope

## Safety defaults

The scaffold runs in safe mode by default:

- safe actions like `open_url`, `open_app`, and `search_web` still execute
- restricted actions remain blocked or require confirmation by policy
- dry-run behavior only happens when a request explicitly asks for simulation
- no external API calls are required for the current milestone
