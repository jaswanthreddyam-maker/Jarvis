from __future__ import annotations

from pathlib import Path

from jarvis.core.tools import ToolResult


def create_file(name: str, *, project_root: Path, content: str = "", overwrite: bool = False) -> ToolResult:
    try:
        file_path = resolve_safe_path(name, project_root)
    except ValueError as exc:
        return ToolResult(success=False, message=str(exc), error="unsafe_path")

    if file_path.exists() and not overwrite:
        return ToolResult(
            success=False,
            message=f"File already exists: {file_path}",
            error="file_exists",
            data={"path": str(file_path)},
        )

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    action = "Overwrote" if overwrite else "Created"
    return ToolResult(
        success=True,
        message=f"{action} {file_path.name}.",
        data={"path": str(file_path), "content": content},
    )


def overwrite_file(name: str, *, project_root: Path, content: str) -> ToolResult:
    return create_file(name=name, project_root=project_root, content=content, overwrite=True)


def read_file(name: str, *, project_root: Path) -> ToolResult:
    max_read_bytes = 128 * 1024
    try:
        file_path = resolve_safe_path(name, project_root)
    except ValueError as exc:
        return ToolResult(success=False, message=str(exc), error="unsafe_path")

    if not file_path.exists() or not file_path.is_file():
        return ToolResult(
            success=False,
            message=f"File not found: {file_path}",
            error="file_not_found",
            data={"path": str(file_path)},
        )

    raw = file_path.read_text(encoding="utf-8", errors="replace")
    encoded = raw.encode("utf-8", errors="replace")
    truncated = len(encoded) > max_read_bytes
    if truncated:
        content = encoded[:max_read_bytes].decode("utf-8", errors="ignore")
    else:
        content = raw

    message = f"Read {file_path.name}."
    if truncated:
        message += " Output was truncated for safety."
    return ToolResult(
        success=True,
        message=message,
        data={"path": str(file_path), "content": content, "truncated": truncated},
    )


def delete_file(name: str, *, project_root: Path) -> ToolResult:
    try:
        file_path = resolve_safe_path(name, project_root)
    except ValueError as exc:
        return ToolResult(success=False, message=str(exc), error="unsafe_path")

    if not file_path.exists() or not file_path.is_file():
        return ToolResult(
            success=False,
            message=f"File not found: {file_path}",
            error="file_not_found",
            data={"path": str(file_path)},
        )

    file_path.unlink()
    return ToolResult(success=True, message=f"Deleted {file_path.name}.", data={"path": str(file_path)})


def _allowed_roots(project_root: Path) -> tuple[Path, ...]:
    home = Path.home()
    candidates = (
        project_root.resolve(),
        (home / "Desktop").resolve(),
        (home / "Documents").resolve(),
        (home / "Downloads").resolve(),
    )
    roots: list[Path] = []
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        roots.append(candidate)
    return tuple(roots)


def resolve_safe_path(name: str, project_root: Path) -> Path:
    raw = str(name).strip().strip('"').strip("'")
    if not raw:
        raise ValueError("A file path is required.")

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = project_root / candidate

    resolved = candidate.resolve(strict=False)
    for root in _allowed_roots(project_root):
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(f"Path '{resolved}' is outside the allowed automation roots.")
