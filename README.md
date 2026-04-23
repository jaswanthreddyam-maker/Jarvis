# Jarvis

Jarvis is now structured as a deployable AI assistant runtime with centralized configuration, structured logging, health monitoring, bounded concurrency, retry/backoff, a standalone backend API, and a separate desktop client.

## Quick start

1. Install the pinned dependencies from `requirements.txt`.
2. Copy `.env.example` to `.env` and fill in the provider keys or local model settings you want.
3. Start the backend service with `python -m jarvis.api`.
4. Start the desktop client in a separate process with `python ui/app.py`.

The desktop client communicates with the backend over the network only. Real-time runtime updates stream over `ws://127.0.0.1:8000/ws`; REST remains available for simple health and execution endpoints.

## Operations

- Structured logs: `logs/jarvis.log`
- Error log: `logs/jarvis.error.log`
- Audit trail: `logs/safety_audit.jsonl`
- Health endpoint: `GET /health`
- Readiness endpoint: `GET /ready`
- Streaming endpoint: `WS /ws`

## Documentation

- [Setup Guide](docs/setup.md)
- [Architecture Overview](docs/architecture.md)
- [Deployment Strategy](docs/deployment.md)

## Test

```powershell
python -m unittest discover -s tests
```
