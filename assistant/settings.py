from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - fallback is exercised in minimal environments
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
        for future_indent, future_text in raw_lines[index + 1:]:
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
        if raw_value:
            value = _parse_scalar(raw_value)
        else:
            value = next_child_container(index, indent)

        if not isinstance(parent, dict):
            raise ValueError("Unsupported YAML mapping structure for fallback parser.")
        parent[key] = value

        if isinstance(value, (dict, list)):
            stack.append((indent, value))

    return root


def load_structured_config(path: Path) -> dict[str, Any]:
    if path.suffix in {".yaml", ".yml"}:
        text = path.read_text(encoding="utf-8")
        if text.lstrip().startswith(("{", "[")):
            return json.loads(text)
        if yaml is not None:
            return yaml.safe_load(text)
        return _simple_yaml_load(text)
    return json.loads(path.read_text(encoding="utf-8"))


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(slots=True)
class Settings:
    project_root: Path
    package_root: Path
    config_root: Path
    memory_db_path: Path
    db_schema_path: Path
    permissions_path: Path
    retry_policy_path: Path
    personality_path: Path
    models_path: Path
    config_path: Path
    audio: dict[str, Any] = field(default_factory=dict)
    safe_mode: bool = False
    simulate_actions: bool = False
    offline_mode: bool = True
    
    _config: dict[str, Any] = field(default_factory=dict)
    
    def get_config(self) -> dict[str, Any]:
        return self._config


def load_settings(memory_db_path: Path | None = None) -> Settings:
    package_root = Path(__file__).resolve().parent
    project_root = package_root.parent
    config_root = package_root / "config"
    load_dotenv(config_root / ".env")

    models_path = config_root / "models.yaml"
    config_path = config_root / "config.yaml"
    
    models = load_structured_config(models_path) if models_path.exists() else {}
    main_config = load_structured_config(config_path) if config_path.exists() else {}
    
    # ── SCHEMA VALIDATION & DEFAULTS ─────────────────────────────────────
    
    def _validate_dict(src: dict, defaults: dict) -> dict:
        validated = src.copy()
        for k, v in defaults.items():
            if k not in validated:
                validated[k] = v
            elif isinstance(v, dict) and isinstance(validated[k], dict):
                validated[k] = _validate_dict(validated[k], v)
            elif type(validated[k]) is not type(v):
                # Fallback to default if type mismatch
                validated[k] = v
        return validated

    DEFAULT_CONFIG = {
        "core": {
            "wake_word": "hey jarvis",
            "confidence_threshold": 0.65,
            "safe_mode": False,
            "simulate_actions": False,
            "fallback_to_local": True
        },
        "performance": {
            "monitor_cpu": True,
            "max_idle_cpu": 65.0,
            "low_perf_fps": 15,
            "high_perf_fps": 30
        },
        "models": {
            "planner": "llama3",
            "online": "gpt-4o-mini",
            "asr": "tiny.en",
            "tts": "vits"
        },
        "audio": {
            "sample_rate": 16000,
            "vad_threshold": 0.01,
            "silence_duration": 1.5,
            "chunk_size": 0.3
        }
    }
    
    main_config = _validate_dict(main_config, DEFAULT_CONFIG)

    # Merge configs (main_config overrides models.yaml)
    audio_cfg = models.get("audio", {})
    audio_cfg.update(main_config.get("audio", {}))
    
    runtime = models.get("runtime", {})
    runtime.update(main_config.get("core", {}))

    resolved_memory_db_path = memory_db_path or (package_root / "memory" / "longterm.db")

    return Settings(
        project_root=project_root,
        package_root=package_root,
        config_root=config_root,
        memory_db_path=resolved_memory_db_path,
        db_schema_path=package_root / "memory" / "db_schema.sql",
        permissions_path=package_root / "permissions.yaml",
        retry_policy_path=package_root / "retry_policy.yaml",
        personality_path=package_root / "personality.json",
        models_path=models_path,
        config_path=config_path,
        audio=audio_cfg,
        safe_mode=os.getenv(
            "JARVIS_SAFE_MODE",
            str(runtime.get("safe_mode", False)),
        ).lower()
        not in {"0", "false", "no"},
        simulate_actions=os.getenv(
            "JARVIS_SIMULATE_ACTIONS",
            str(runtime.get("simulate_actions", False)),
        ).lower()
        not in {"0", "false", "no"},
        offline_mode=os.getenv(
            "JARVIS_OFFLINE_MODE",
            str(models.get("safety", {}).get("local_first", True)),
        ).lower()
        not in {"0", "false", "no"},
        _config=main_config,
    )
