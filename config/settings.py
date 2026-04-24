from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.env_loader import load_dotenv, load_environment

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


def _parse_scalar(raw_value: str) -> Any:
    value = raw_value.strip()
    if not value:
        return ""
    if value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]

    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None

    try:
        return int(value)
    except ValueError:
        pass

    try:
        return float(value)
    except ValueError:
        return value


def _simple_yaml_load(text: str) -> dict[str, Any]:
    raw_lines = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        raw_lines.append((len(raw_line) - len(raw_line.lstrip(" ")), stripped))

    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    def next_child_container(index: int, current_indent: int) -> Any:
        for future_indent, future_text in raw_lines[index + 1 :]:
            if future_indent <= current_indent:
                break
            return [] if future_text.startswith("- ") else {}
        return {}

    for index, (indent, stripped) in enumerate(raw_lines):
        while indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]
        if stripped.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError("Unsupported YAML structure for fallback parser.")
            parent.append(_parse_scalar(stripped[2:]))
            continue

        key, _, raw_value = stripped.partition(":")
        if not _:
            raise ValueError(f"Invalid YAML line: {stripped}")

        key = key.strip()
        raw_value = raw_value.strip()
        value = _parse_scalar(raw_value) if raw_value else next_child_container(index, indent)

        if not isinstance(parent, dict):
            raise ValueError("Unsupported YAML mapping structure for fallback parser.")
        parent[key] = value

        if isinstance(value, (dict, list)):
            stack.append((indent, value))

    return root


def load_structured_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.suffix in {".yaml", ".yml"}:
        text = path.read_text(encoding="utf-8")
        if text.lstrip().startswith(("{", "[")):
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else {}
        if yaml is not None:
            payload = yaml.safe_load(text)
            return payload if isinstance(payload, dict) else {}
        return _simple_yaml_load(text)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_str(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default)).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _resolve_path(raw_value: str | Path, *, project_root: Path) -> Path:
    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


@dataclass(slots=True)
class LoggingSettings:
    level: str
    console_enabled: bool
    console_json: bool
    directory: Path
    app_log_path: Path
    error_log_path: Path
    audit_log_path: Path
    max_bytes: int
    backup_count: int


@dataclass(slots=True)
class ProviderSettings:
    openai_api_key: str
    openai_model: str
    openai_base_url: str
    anthropic_api_key: str
    anthropic_model: str
    ollama_url: str
    ollama_model: str
    max_tokens: int
    request_timeout_seconds: float


@dataclass(slots=True)
class RetrySettings:
    max_attempts: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    jitter_seconds: float


@dataclass(slots=True)
class ApiSettings:
    enabled: bool
    host: str
    port: int
    request_timeout_seconds: float


@dataclass(slots=True)
class HealthSettings:
    enabled: bool
    host: str
    port: int
    api_timeout_seconds: float
    check_external_connectivity: bool
    memory_warning_mb: float
    startup_grace_seconds: float
    heartbeat_timeout_seconds: float
    poll_interval_seconds: float
    retry_reset_seconds: float
    max_backoff_seconds: float


@dataclass(slots=True)
class ResourceSettings:
    max_concurrent_requests: int
    max_pending_requests: int
    max_memory_mb: float


@dataclass(slots=True)
class Settings:
    environment: str
    debug: bool
    project_root: Path
    package_root: Path
    config_root: Path
    docs_root: Path
    logs_root: Path
    memory_db_path: Path
    db_schema_path: Path
    permissions_path: Path
    retry_policy_path: Path
    personality_path: Path
    models_path: Path
    config_path: Path
    audio: dict[str, Any] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    safe_mode: bool = False
    simulate_actions: bool = False
    offline_mode: bool = True
    logging: LoggingSettings = field(default_factory=lambda: LoggingSettings("", False, False, Path("."), Path("."), Path("."), Path("."), 0, 0))
    providers: ProviderSettings = field(default_factory=lambda: ProviderSettings("", "", "", "", "", "", "", 0, 0.0))
    retry: RetrySettings = field(default_factory=lambda: RetrySettings(1, 0.0, 0.0, 0.0))
    api: ApiSettings = field(default_factory=lambda: ApiSettings(False, "127.0.0.1", 8000, 30.0))
    health: HealthSettings = field(default_factory=lambda: HealthSettings(True, "127.0.0.1", 8765, 5.0, False, 512.0, 30.0, 10.0, 2.0, 60.0, 120.0))
    resources: ResourceSettings = field(default_factory=lambda: ResourceSettings(2, 8, 1024.0))
    loaded_env_files: tuple[Path, ...] = field(default_factory=tuple)
    _config: dict[str, Any] = field(default_factory=dict)

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)


_DEFAULT_CONFIG: dict[str, Any] = {
    "core": {
        "confidence_threshold": 0.65,
        "safe_mode": False,
        "simulate_actions": False,
    },
    "performance": {
        "monitor_cpu": True,
        "max_idle_cpu": 65.0,
        "low_perf_fps": 15,
        "high_perf_fps": 30,
    },
    "models": {
        "planner": "llama3",
        "online": "gpt-4o-mini",
        "asr": "base.en",
        "tts": "vits",
        "semantic_embedding": "all-MiniLM-L6-v2",
    },
    "audio": {
        "sample_rate": 16000,
        "vad_threshold": 0.01,
        "silence_duration": 1.5,
        "chunk_size": 0.3,
        "whisper_model": "base.en",
        "background_wake_enabled": False,
        "background_wake_interval_seconds": 1.75,
    },
    "logging": {
        "max_bytes": 10 * 1024 * 1024,
        "backup_count": 5,
        "console_json": False,
    },
    "runtime": {
        "max_concurrent_requests": 2,
        "max_pending_requests": 8,
        "request_timeout_seconds": 30.0,
    },
    "health": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
        "api_timeout_seconds": 5.0,
        "check_external_connectivity": False,
        "memory_warning_mb": 512.0,
        "startup_grace_seconds": 30.0,
        "heartbeat_timeout_seconds": 10.0,
        "poll_interval_seconds": 2.0,
        "retry_reset_seconds": 60.0,
        "max_backoff_seconds": 120.0,
    },
    "resources": {
        "max_memory_mb": 1024.0,
    },
    "retry": {
        "max_attempts": 3,
        "backoff_base_seconds": 0.5,
        "backoff_max_seconds": 8.0,
        "jitter_seconds": 0.2,
    },
}


def load_settings(memory_db_path: Path | None = None) -> Settings:
    project_root = Path(__file__).resolve().parents[1]
    package_root = project_root / "jarvis"
    config_root = project_root / "config"
    docs_root = project_root / "docs"

    env_result = load_environment(project_root)
    environment = env_result.environment
    debug_default = environment != "production"

    load_dotenv(config_root / ".env")

    config_path = _resolve_path(
        _env_str("JARVIS_CONFIG_YAML", str(config_root / "config.yaml")),
        project_root=project_root,
    )
    models_path = _resolve_path(
        _env_str("JARVIS_MODELS_YAML", str(config_root / "models.yaml")),
        project_root=project_root,
    )

    models_file = load_structured_config(models_path)
    app_file = load_structured_config(config_path)
    merged_config = _deep_merge(_DEFAULT_CONFIG, models_file)
    merged_config = _deep_merge(merged_config, app_file)

    model_config = dict(merged_config.get("models", {}) or {})
    audio_config = dict(_deep_merge(dict(merged_config.get("audio", {}) or {}), {}))

    debug = _env_bool("JARVIS_DEBUG", debug_default)
    logs_root = _resolve_path(
        _env_str("JARVIS_LOG_DIR", str(project_root / "logs")),
        project_root=project_root,
    )
    logs_root.mkdir(parents=True, exist_ok=True)

    resolved_memory_db_path = (
        Path(memory_db_path).expanduser().resolve()
        if memory_db_path is not None
        else _resolve_path(
            _env_str("JARVIS_MEMORY_DB_PATH", str(project_root / "data" / "longterm.db")),
            project_root=project_root,
        )
    )
    resolved_memory_db_path.parent.mkdir(parents=True, exist_ok=True)

    db_schema_path = _resolve_path(
        _env_str("JARVIS_DB_SCHEMA_PATH", str(config_root / "db_schema.sql")),
        project_root=project_root,
    )
    permissions_path = _resolve_path(
        _env_str("JARVIS_PERMISSIONS_PATH", str(config_root / "permissions.yaml")),
        project_root=project_root,
    )
    retry_policy_path = _resolve_path(
        _env_str("JARVIS_RETRY_POLICY_PATH", str(config_root / "retry_policy.yaml")),
        project_root=project_root,
    )
    personality_path = _resolve_path(
        _env_str("JARVIS_PERSONALITY_PATH", str(config_root / "personality.json")),
        project_root=project_root,
    )

    log_level_default = "DEBUG" if debug else ("INFO" if environment == "production" else "DEBUG")
    log_level = _env_str("JARVIS_LOG_LEVEL", log_level_default).upper()
    console_logging = _env_bool("JARVIS_CONSOLE_LOGGING", environment != "production")
    console_json = _env_bool(
        "JARVIS_CONSOLE_JSON",
        bool(merged_config.get("logging", {}).get("console_json", False)),
    )
    max_bytes = _env_int(
        "JARVIS_LOG_MAX_BYTES",
        int(merged_config.get("logging", {}).get("max_bytes", 10 * 1024 * 1024)),
        minimum=1024,
    )
    backup_count = _env_int(
        "JARVIS_LOG_BACKUP_COUNT",
        int(merged_config.get("logging", {}).get("backup_count", 5)),
        minimum=1,
    )

    runtime_config = dict(merged_config.get("runtime", {}) or {})
    health_config = dict(merged_config.get("health", {}) or {})
    resources_config = dict(merged_config.get("resources", {}) or {})
    retry_config = dict(merged_config.get("retry", {}) or {})

    settings = Settings(
        environment=environment,
        debug=debug,
        project_root=project_root,
        package_root=package_root,
        config_root=config_root,
        docs_root=docs_root,
        logs_root=logs_root,
        memory_db_path=resolved_memory_db_path,
        db_schema_path=db_schema_path,
        permissions_path=permissions_path,
        retry_policy_path=retry_policy_path,
        personality_path=personality_path,
        models_path=models_path,
        config_path=config_path,
        audio=audio_config,
        models={
            **model_config,
            "planner": _env_str("JARVIS_PLANNER_MODEL", str(model_config.get("planner", "llama3"))),
            "online": _env_str("OPENAI_MODEL", str(model_config.get("online", "gpt-4o-mini"))),
            "asr": _env_str("JARVIS_ASR_MODEL", str(model_config.get("asr", "base.en"))),
            "tts": _env_str("JARVIS_TTS_MODEL", str(model_config.get("tts", "vits"))),
            "semantic_embedding": _env_str(
                "JARVIS_SEMANTIC_EMBEDDING_MODEL",
                str(model_config.get("semantic_embedding", "all-MiniLM-L6-v2")),
            ),
        },
        safe_mode=_env_bool("JARVIS_SAFE_MODE", bool(merged_config.get("core", {}).get("safe_mode", False))),
        simulate_actions=_env_bool(
            "JARVIS_SIMULATE_ACTIONS",
            bool(merged_config.get("core", {}).get("simulate_actions", False)),
        ),
        offline_mode=_env_bool("JARVIS_OFFLINE_MODE", environment != "production"),
        logging=LoggingSettings(
            level=log_level,
            console_enabled=console_logging,
            console_json=console_json,
            directory=logs_root,
            app_log_path=logs_root / "jarvis.log",
            error_log_path=logs_root / "jarvis.error.log",
            audit_log_path=logs_root / "safety_audit.jsonl",
            max_bytes=max_bytes,
            backup_count=backup_count,
        ),
        providers=ProviderSettings(
            openai_api_key=_env_str("OPENAI_API_KEY"),
            openai_model=_env_str("OPENAI_MODEL", str(model_config.get("online", "gpt-4o-mini"))),
            openai_base_url=_env_str("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            anthropic_api_key=_env_str("ANTHROPIC_API_KEY"),
            anthropic_model=_env_str("ANTHROPIC_MODEL", "claude-3-haiku-20240307"),
            ollama_url=_env_str("OLLAMA_URL", "http://127.0.0.1:11434"),
            ollama_model=_env_str("OLLAMA_MODEL", str(model_config.get("planner", "llama3.2"))),
            max_tokens=_env_int("JARVIS_PROVIDER_MAX_TOKENS", 150, minimum=1),
            request_timeout_seconds=_env_float(
                "JARVIS_PROVIDER_TIMEOUT_SECONDS",
                float(runtime_config.get("request_timeout_seconds", 30.0)),
                minimum=1.0,
            ),
        ),
        retry=RetrySettings(
            max_attempts=_env_int(
                "JARVIS_RETRY_MAX_ATTEMPTS",
                int(retry_config.get("max_attempts", 3)),
                minimum=1,
            ),
            backoff_base_seconds=_env_float(
                "JARVIS_RETRY_BACKOFF_BASE_SECONDS",
                float(retry_config.get("backoff_base_seconds", 0.5)),
                minimum=0.0,
            ),
            backoff_max_seconds=_env_float(
                "JARVIS_RETRY_BACKOFF_MAX_SECONDS",
                float(retry_config.get("backoff_max_seconds", 8.0)),
                minimum=0.1,
            ),
            jitter_seconds=_env_float(
                "JARVIS_RETRY_JITTER_SECONDS",
                float(retry_config.get("jitter_seconds", 0.2)),
                minimum=0.0,
            ),
        ),
        api=ApiSettings(
            enabled=_env_bool("JARVIS_API_ENABLED", False),
            host=_env_str("JARVIS_API_HOST", "127.0.0.1"),
            port=_env_int("JARVIS_API_PORT", 8000, minimum=1),
            request_timeout_seconds=_env_float(
                "JARVIS_API_REQUEST_TIMEOUT_SECONDS",
                float(runtime_config.get("request_timeout_seconds", 30.0)),
                minimum=1.0,
            ),
        ),
        health=HealthSettings(
            enabled=_env_bool("JARVIS_HEALTH_ENABLED", bool(health_config.get("enabled", True))),
            host=_env_str("JARVIS_HEALTH_HOST", str(health_config.get("host", "127.0.0.1"))),
            port=_env_int("JARVIS_HEALTH_PORT", int(health_config.get("port", 8765)), minimum=1),
            api_timeout_seconds=_env_float(
                "JARVIS_HEALTH_API_TIMEOUT_SECONDS",
                float(health_config.get("api_timeout_seconds", 5.0)),
                minimum=0.1,
            ),
            check_external_connectivity=_env_bool(
                "JARVIS_HEALTH_CHECK_EXTERNAL_CONNECTIVITY",
                bool(health_config.get("check_external_connectivity", False)),
            ),
            memory_warning_mb=_env_float(
                "JARVIS_HEALTH_MEMORY_WARNING_MB",
                float(health_config.get("memory_warning_mb", 512.0)),
                minimum=1.0,
            ),
            startup_grace_seconds=_env_float(
                "JARVIS_HEALTH_STARTUP_GRACE_SECONDS",
                float(health_config.get("startup_grace_seconds", 30.0)),
                minimum=0.0,
            ),
            heartbeat_timeout_seconds=_env_float(
                "JARVIS_HEALTH_HEARTBEAT_TIMEOUT_SECONDS",
                float(health_config.get("heartbeat_timeout_seconds", 10.0)),
                minimum=0.1,
            ),
            poll_interval_seconds=_env_float(
                "JARVIS_HEALTH_POLL_INTERVAL_SECONDS",
                float(health_config.get("poll_interval_seconds", 2.0)),
                minimum=0.1,
            ),
            retry_reset_seconds=_env_float(
                "JARVIS_HEALTH_RETRY_RESET_SECONDS",
                float(health_config.get("retry_reset_seconds", 60.0)),
                minimum=0.1,
            ),
            max_backoff_seconds=_env_float(
                "JARVIS_HEALTH_MAX_BACKOFF_SECONDS",
                float(health_config.get("max_backoff_seconds", 120.0)),
                minimum=0.1,
            ),
        ),
        resources=ResourceSettings(
            max_concurrent_requests=_env_int(
                "JARVIS_MAX_CONCURRENT_REQUESTS",
                int(runtime_config.get("max_concurrent_requests", 2)),
                minimum=1,
            ),
            max_pending_requests=_env_int(
                "JARVIS_MAX_PENDING_REQUESTS",
                int(runtime_config.get("max_pending_requests", 8)),
                minimum=1,
            ),
            max_memory_mb=_env_float(
                "JARVIS_MAX_MEMORY_MB",
                float(resources_config.get("max_memory_mb", 1024.0)),
                minimum=32.0,
            ),
        ),
        loaded_env_files=env_result.loaded_files,
        _config=merged_config,
    )

    return settings
