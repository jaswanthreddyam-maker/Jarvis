from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class EnvLoadResult:
    environment: str
    loaded_files: tuple[Path, ...]


def load_dotenv(path: Path, *, override: bool = False) -> dict[str, str]:
    if not path.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def load_environment(
    project_root: Path,
    *,
    environment: str | None = None,
    override: bool = False,
) -> EnvLoadResult:
    resolved_environment = str(
        environment
        or os.environ.get("JARVIS_ENV")
        or os.environ.get("APP_ENV")
        or "development"
    ).strip().lower() or "development"

    explicit_env_file = os.environ.get("JARVIS_ENV_FILE", "").strip()
    candidates = [project_root / ".env", project_root / f".env.{resolved_environment}"]
    if explicit_env_file:
        candidates.append(Path(explicit_env_file).expanduser())

    loaded_files: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        load_dotenv(candidate, override=override)
        loaded_files.append(candidate.resolve())

    os.environ.setdefault("JARVIS_ENV", resolved_environment)
    return EnvLoadResult(
        environment=resolved_environment,
        loaded_files=tuple(loaded_files),
    )
