# Deployment Strategy

## Recommended modes

- Desktop mode
  Use `python main.py` when you need the local UI and voice workflow on a single workstation.
- API mode
  Use `uvicorn jarvis.api.app:create_app --factory --host 0.0.0.0 --port 8000` for service deployment, automation, or remote UI integration.
- Container mode
  Build with `docker build -t jarvis .` and run with the production env file mounted or injected.

## Process management

- `docker/systemd/jarvis.service`
  For Linux hosts that should restart Jarvis automatically after crashes or reboots.
- `docker/supervisor/jarvis.conf`
  For supervisor-managed deployments where stdout/stderr log routing matters.

## Operational checks

- `/health`
  Full health snapshot with process memory, runtime state, and provider connectivity summary.
- `/ready`
  Readiness probe for load balancers or orchestrators.
- `logs/jarvis.log`
  Primary structured application log.
- `logs/safety_audit.jsonl`
  Append-only audit trail for user input, permissions, and execution decisions.
