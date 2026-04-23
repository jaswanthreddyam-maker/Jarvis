# Safety Layer

Jarvis now routes every executable action through a dedicated safety pipeline before any tool handler can touch the system.

## Execution Flow

User input -> LLM plan -> action registry lookup -> schema validation -> scope validation -> permission check -> sandbox policy -> guard checks -> timed execution -> audit log

## Core Modules

- `jarvis/core/safety/validator.py`
  - Verifies the tool exists in the registry.
  - Enforces per-tool argument schemas.
  - Sanitizes browser names, file paths, URLs, and other high-risk inputs.
  - Blocks raw shell-style tools such as `run_command`, `shell_exec`, and `run_code`.

- `jarvis/core/safety/permissions.py`
  - Defines permission levels: `SAFE`, `MODERATE`, and `DANGEROUS`.
  - Maps built-in actions to a permission level.
  - Lets execution combine tool risk and policy overrides into one decision.

- `jarvis/core/safety/sandbox.py`
  - Restricts file operations to approved roots.
  - Rejects blocked URL schemes such as `javascript:` and `file:`.
  - Requires browser launches to use an approved browser alias.
  - Enforces YouTube-only hosts for direct playback URLs.

- `jarvis/core/safety/guard.py`
  - Applies action rate limits.
  - Assigns per-action execution timeouts.
  - Cancels long-running tasks via the cancellation controller.
  - Provides the emergency-stop mechanism.

- `jarvis/core/safety/logger.py`
  - Writes append-only JSONL audit records.
  - Captures user input, plan creation, validation outcomes, permission events, action execution, timeouts, and emergency stops.

## Permission Model

- `SAFE`
  - Read-like or reversible actions such as `open_url`, `search_web`, `get_time`, and `read_file`.

- `MODERATE`
  - State-changing but bounded actions such as `create_file`, `overwrite_file`, `set_clipboard`, and `set_reminder`.

- `DANGEROUS`
  - Destructive or system-level actions such as `delete_file`, `install_app`, and `system_action`.

Dangerous actions require confirmation. Safe mode blocks dangerous actions entirely.

## Tool Whitelist

Only actions that are explicitly registered in `ActionRegistry` can execute.

Even if a raw shell tool is registered, the validator blocks known shell-style actions:

- `run_command`
- `shell_exec`
- `shell_execution`
- `run_code`

## Emergency Stop

`emergency_stop` cancels all active request tokens through the shared cancellation controller and records the event in the audit log.

## Audit Trail

Bootstrap now attaches a shared audit logger at:

- `logs/safety_audit.jsonl`

Each line is a JSON event record that can be searched or ingested by external monitoring.
