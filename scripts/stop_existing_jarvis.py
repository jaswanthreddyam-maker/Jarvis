from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

import psutil


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _safe_resolve(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    try:
        return Path(raw_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_under_project(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        path.relative_to(PROJECT_ROOT)
        return True
    except ValueError:
        return False


def _command_text(process: psutil.Process) -> str:
    try:
        return " ".join(process.cmdline())
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ""


def _process_path(process: psutil.Process, attr: str) -> Path | None:
    try:
        value = process.exe() if attr == "exe" else process.cwd()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    return _safe_resolve(value)


def _is_current_launcher_family(process: psutil.Process) -> bool:
    current = psutil.Process(os.getpid())
    blocked = {current.pid}
    try:
        blocked.update(parent.pid for parent in current.parents())
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        pass
    return process.pid in blocked


def _matches_jarvis_process(process: psutil.Process) -> bool:
    if _is_current_launcher_family(process):
        return False

    try:
        name = process.name().lower()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return False

    cmd = _command_text(process)
    normalized_cmd = cmd.lower().replace("/", "\\")
    exe_path = _process_path(process, "exe")
    cwd_path = _process_path(process, "cwd")
    project_related = (
        _is_under_project(exe_path)
        or _is_under_project(cwd_path)
        or str(PROJECT_ROOT).lower() in normalized_cmd
    )
    if not project_related:
        return False

    if name in {"python.exe", "pythonw.exe"}:
        return (
            "-m jarvis.api" in normalized_cmd
            or "jarvis.api.app" in normalized_cmd
            or "ui\\app.py" in normalized_cmd
        )

    return name == "jarvis.exe" and _is_under_project(exe_path)


def _find_targets() -> list[psutil.Process]:
    targets: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if _matches_jarvis_process(process):
                targets.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return sorted(targets, key=lambda item: item.pid)


def _describe(process: psutil.Process) -> str:
    try:
        return f"{process.pid} {process.name()}"
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return str(process.pid)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop existing Jarvis backend/UI processes for this workspace.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    targets = _find_targets()
    if not targets:
        print("[BOOT] No existing Jarvis processes found.")
        return 0

    for process in targets:
        print(f"[BOOT] Existing Jarvis process: {_describe(process)}")
    if args.dry_run:
        return 0

    for process in targets:
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            pass

    _, alive = psutil.wait_procs(targets, timeout=max(0.5, args.timeout))
    for process in alive:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            pass

    if alive:
        psutil.wait_procs(alive, timeout=2.0)
    time.sleep(0.2)
    still_running = []
    for process in alive:
        try:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                still_running.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    if still_running:
        for process in still_running:
            print(f"[ERROR] Could not stop Jarvis process: {_describe(process)}", file=sys.stderr)
        return 1
    print(f"[BOOT] Stopped {len(targets)} existing Jarvis process(es).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
