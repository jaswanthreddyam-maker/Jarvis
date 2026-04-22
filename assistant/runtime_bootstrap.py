from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = PROJECT_ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
PYVENV_CFG = VENV_DIR / "pyvenv.cfg"
RECOMMENDED_MAJOR_MINOR = (3, 10)
REQUIRED_MODULES = ("PySide6", "numpy", "sounddevice", "whisper", "yaml")


def _default_console(message: str) -> None:
    print(message, flush=True)


@dataclass(slots=True)
class InterpreterInfo:
    path: Path
    version: tuple[int, int, int]
    missing_modules: tuple[str, ...]

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)

    @property
    def is_recommended(self) -> bool:
        return self.version[:2] == RECOMMENDED_MAJOR_MINOR

    @property
    def is_healthy(self) -> bool:
        return not self.missing_modules


@dataclass(slots=True)
class RuntimeBootstrapResult:
    python_path: Path
    interpreter: InterpreterInfo
    changed: bool = False
    installed_requirements: bool = False
    recreated_venv: bool = False


def _inspect_python(python_path: Path) -> InterpreterInfo | None:
    if not python_path.exists():
        return None

    probe = (
        "import importlib.util, json, sys; "
        f"mods={REQUIRED_MODULES!r}; "
        "missing=[name for name in mods if importlib.util.find_spec(name) is None]; "
        "print(json.dumps({'version': list(sys.version_info[:3]), 'missing': missing}))"
    )

    try:
        completed = subprocess.run(
            [str(python_path), "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(PROJECT_ROOT),
        )
        payload = json.loads(completed.stdout.strip())
        version = tuple(int(part) for part in payload.get("version", [0, 0, 0]))
        missing = tuple(str(name) for name in payload.get("missing", []))
        return InterpreterInfo(path=python_path.resolve(), version=version, missing_modules=missing)
    except Exception:
        return None


def _read_venv_home() -> Path | None:
    if not PYVENV_CFG.exists():
        return None

    try:
        for line in PYVENV_CFG.read_text(encoding="utf-8").splitlines():
            if not line.lower().startswith("home = "):
                continue
            home = line.split("=", 1)[1].strip()
            if not home:
                return None
            python_path = Path(home) / "python.exe"
            return python_path if python_path.exists() else None
    except Exception:
        return None
    return None


def _iter_candidate_pythons() -> list[Path]:
    seen: set[str] = set()
    candidates: list[Path] = []

    def add(path_like: str | Path | None) -> None:
        if not path_like:
            return
        path = Path(path_like)
        key = os.path.normcase(str(path))
        if key in seen or not path.exists():
            return
        seen.add(key)
        candidates.append(path)

    add(VENV_PYTHON)
    add(_read_venv_home())
    add(sys.executable)

    appdata = os.environ.get("APPDATA")
    if appdata:
        uv_root = Path(appdata) / "uv" / "python"
        preferred = [
            uv_root / "cpython-3.10-windows-x86_64-none" / "python.exe",
            uv_root / "cpython-3.10.20-windows-x86_64-none" / "python.exe",
        ]
        for item in preferred:
            add(item)
        if uv_root.exists():
            for item in sorted(uv_root.glob("cpython-3.10*")):
                add(item / "python.exe")
            for item in sorted(uv_root.glob("cpython-3.11*")):
                add(item / "python.exe")
            for item in sorted(uv_root.glob("cpython-3.12*")):
                add(item / "python.exe")

    return candidates


def _pick_base_interpreter() -> InterpreterInfo | None:
    best: InterpreterInfo | None = None
    for candidate in _iter_candidate_pythons():
        info = _inspect_python(candidate)
        if info is None:
            continue
        if best is None:
            best = info
            continue
        best_key = (
            1 if best.is_recommended else 0,
            1 if best.is_healthy else 0,
            best.version,
        )
        current_key = (
            1 if info.is_recommended else 0,
            1 if info.is_healthy else 0,
            info.version,
        )
        if current_key > best_key:
            best = info
    return best


def _run_checked(command: list[str], console: Callable[[str], None]) -> None:
    console(f"[Jarvis] Running: {' '.join(command)}")
    subprocess.run(command, cwd=str(PROJECT_ROOT), check=True)


def _install_requirements(console: Callable[[str], None]) -> None:
    if not VENV_PYTHON.exists():
        raise RuntimeError("The project virtual environment was not created.")
    _run_checked([str(VENV_PYTHON), "-m", "pip", "install", "-r", "requirements.txt"], console)


def _create_or_repair_venv(base_python: Path, *, console: Callable[[str], None]) -> None:
    command = [str(base_python), "-m", "venv"]
    if VENV_DIR.exists():
        command.append("--clear")
    command.append(str(VENV_DIR))
    _run_checked(command, console)


def ensure_project_runtime(
    *,
    console: Callable[[str], None] | None = None,
    repair: bool = True,
    install_missing: bool = True,
) -> RuntimeBootstrapResult:
    emit = console or _default_console

    current = _inspect_python(VENV_PYTHON)
    if current is not None and current.is_healthy:
        return RuntimeBootstrapResult(python_path=current.path, interpreter=current)

    if not repair:
        failing = current or InterpreterInfo(
            path=VENV_PYTHON,
            version=(0, 0, 0),
            missing_modules=REQUIRED_MODULES,
        )
        return RuntimeBootstrapResult(python_path=failing.path, interpreter=failing)

    base = _pick_base_interpreter()
    if base is None:
        raise RuntimeError(
            "Jarvis could not find a usable Python interpreter. "
            "Install Python 3.10 or restore your uv-managed runtime."
        )

    emit(f"[Jarvis] Using bootstrap interpreter: {base.path} ({base.version_text})")
    recreated = current is None or current.path != base.path or not current.is_healthy
    if recreated:
        _create_or_repair_venv(base.path, console=emit)

    post_create = _inspect_python(VENV_PYTHON)
    if post_create is None:
        raise RuntimeError("Jarvis could not validate the repaired virtual environment.")

    installed_requirements = False
    if install_missing and post_create.missing_modules:
        emit(
            "[Jarvis] Installing missing packages into the project environment: "
            + ", ".join(post_create.missing_modules)
        )
        _install_requirements(console=emit)
        installed_requirements = True
        post_create = _inspect_python(VENV_PYTHON)
        if post_create is None or post_create.missing_modules:
            missing = ", ".join(post_create.missing_modules) if post_create else "unknown modules"
            raise RuntimeError(f"Jarvis runtime is still incomplete after repair: {missing}")

    return RuntimeBootstrapResult(
        python_path=post_create.path,
        interpreter=post_create,
        changed=recreated or installed_requirements,
        installed_requirements=installed_requirements,
        recreated_venv=recreated,
    )


def bootstrap_and_exit() -> int:
    try:
        result = ensure_project_runtime()
    except Exception as exc:
        _default_console(f"[Jarvis] Runtime bootstrap failed: {exc}")
        return 1

    status = "healthy"
    if result.recreated_venv:
        status = "recreated"
    elif result.installed_requirements:
        status = "repaired"
    _default_console(
        f"[Jarvis] Project runtime is {status}: {result.python_path} "
        f"({result.interpreter.version_text})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(bootstrap_and_exit())
