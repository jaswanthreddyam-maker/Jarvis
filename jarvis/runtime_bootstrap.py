from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


@dataclass(slots=True)
class RuntimeBootstrapResult:
    python_path: Path


def ensure_project_runtime(console=None) -> RuntimeBootstrapResult:
    del console
    return RuntimeBootstrapResult(python_path=Path(sys.executable).resolve())

