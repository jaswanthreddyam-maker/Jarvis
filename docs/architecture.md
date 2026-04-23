# Architecture Overview

Jarvis now has a production-oriented control plane around the existing assistant runtime:

- `config/`
  Central environment loading and validated runtime settings.
- `jarvis/observability/`
  Structured logging and event-bus log bridging for request, execution, and safety events.
- `jarvis/monitoring/`
  Health snapshots covering process memory, runtime state, and provider reachability.
- `jarvis/api/`
  Thin FastAPI wrapper exposing `/health`, `/ready`, and `/v1/execute`.
- `jarvis/application/`
  Application bootstrap, orchestration wiring, and handler registration.
- `jarvis/runtime/`
  Runtime controller, bounded concurrency, timeouts, and graceful shutdown behavior.

The desktop UI still runs through the same core application graph, so the production hardening is shared between local desktop and server deployments.
