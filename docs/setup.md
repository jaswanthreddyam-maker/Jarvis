# Setup Guide

## Local development

1. Create a virtual environment and install the pinned dependencies from `requirements.txt`.
2. Copy `.env.example` to `.env`, then fill in provider keys and any path overrides you need.
3. Start the API runtime with `python -m jarvis.api`.
4. Start the desktop client separately with `python ui/app.py`.

## Configuration model

- Base configuration is loaded from `.env` and optional profile files like `.env.development` or `.env.production`.
- Runtime defaults are defined in `config/settings.py` and can be overridden by environment variables.
- Environment variables always win over file defaults.

## Logs

- Structured JSON logs are written to `logs/jarvis.log`.
- Error-only logs are written to `logs/jarvis.error.log`.
- Safety/audit events are written to `logs/safety_audit.jsonl`.
