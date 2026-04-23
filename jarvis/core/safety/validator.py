from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from jarvis.config.constants import BROWSER_APPS, KNOWN_FOLDERS, SYSTEM_ACTIONS
from jarvis.core.safety.permissions import PermissionLevel, permission_level_for
from jarvis.core.tools import ToolRegistry


_CONTROL_PARAMS = frozenset({"simulate", "dry_run"})
_PROHIBITED_ACTIONS = frozenset({"run_code", "run_command", "shell_exec", "shell_execution"})
_SAFE_TEXT_MAX = 512
_LARGE_TEXT_MAX = 20_000
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9 ._:-]{0,127}$", re.IGNORECASE)
_MEMORY_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,63}$", re.IGNORECASE)
_DANGEROUS_TEXT_MARKERS = ("&&", "||", "$(", "`", "\x00", "\r", "\n")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    kind: str = "string"
    required: bool = False
    allow_empty: bool = False
    max_length: int = _SAFE_TEXT_MAX
    min_value: int | float | None = None
    max_value: int | float | None = None
    allowed_values: tuple[str, ...] = ()


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    sanitized_params: dict[str, Any] = field(default_factory=dict)
    reason: str = "Validated."
    normalized_target: str = ""
    permission_level: PermissionLevel = PermissionLevel.SAFE


class ToolValidator:
    """Validates tool existence, argument schema, and basic input sanitization."""

    _FIELD_SCHEMAS: dict[str, tuple[FieldSpec, ...]] = {
        "close_app": (FieldSpec("app_name", kind="identifier", required=True),),
        "close_active_window": (),
        "create_file": (
            FieldSpec("name", kind="path", required=True, max_length=260),
            FieldSpec("content", kind="text", max_length=_LARGE_TEXT_MAX),
        ),
        "delete_file": (FieldSpec("name", kind="path", required=True, max_length=260),),
        "emergency_stop": (),
        "focus_app": (FieldSpec("app_name", kind="identifier", required=True),),
        "get_clipboard": (),
        "get_time": (),
        "health_check": (),
        "install_app": (FieldSpec("app_name", kind="identifier", required=True),),
        "minimize_window": (),
        "open_app": (FieldSpec("app_name", kind="identifier", required=True),),
        "open_explorer": (FieldSpec("path", kind="path_or_folder", max_length=260),),
        "open_url": (
            FieldSpec("url", kind="url_or_alias", required=True, max_length=1024),
            FieldSpec("browser_app", kind="browser"),
        ),
        "overwrite_file": (
            FieldSpec("name", kind="path", required=True, max_length=260),
            FieldSpec("content", kind="text", required=True, allow_empty=True, max_length=_LARGE_TEXT_MAX),
        ),
        "play_youtube": (
            FieldSpec("query", kind="query", required=True, max_length=256),
            FieldSpec("browser_app", kind="browser"),
            FieldSpec("video_url", kind="url", max_length=1024),
        ),
        "read_file": (FieldSpec("name", kind="path", required=True, max_length=260),),
        "recall_memory": (
            FieldSpec("query", kind="string", max_length=256),
            FieldSpec("namespace", kind="memory_namespace", max_length=64),
        ),
        "remember_fact": (
            FieldSpec("content", kind="text", required=True, max_length=_LARGE_TEXT_MAX),
            FieldSpec("namespace", kind="memory_namespace", max_length=64),
            FieldSpec("category", kind="memory_namespace", max_length=64),
        ),
        "report_capabilities": (),
        "save_preference": (
            FieldSpec("key", kind="memory_namespace", required=True, max_length=64),
            FieldSpec("value", kind="text", required=True, allow_empty=False, max_length=_LARGE_TEXT_MAX),
        ),
        "search_web": (
            FieldSpec("query", kind="query", required=True, max_length=256),
            FieldSpec("browser_app", kind="browser"),
        ),
        "search_youtube": (
            FieldSpec("query", kind="query", required=True, max_length=256),
            FieldSpec("browser_app", kind="browser"),
        ),
        "set_clipboard": (FieldSpec("text", kind="text", required=True, max_length=_LARGE_TEXT_MAX),),
        "set_reminder": (
            FieldSpec("message", kind="text", required=True, max_length=512),
            FieldSpec("delay_seconds", kind="integer", required=True, min_value=0, max_value=604800),
        ),
        "set_volume": (
            FieldSpec(
                "value",
                kind="enum",
                required=True,
                allowed_values=("increase", "up", "raise", "louder", "decrease", "down", "lower", "quieter", "mute", "silence"),
            ),
        ),
        "switch_window": (FieldSpec("title", kind="string", required=True, max_length=200),),
        "system_action": (
            FieldSpec("action_type", kind="enum", required=True, allowed_values=tuple(sorted(SYSTEM_ACTIONS))),
        ),
    }

    def validate(
        self,
        *,
        registry: ToolRegistry,
        action_name: str,
        params: dict[str, Any] | None,
        target: str = "",
        project_root: Path | None = None,
    ) -> ValidationResult:
        del project_root
        normalized_action = str(action_name).strip().lower()
        if not normalized_action:
            return ValidationResult(valid=False, reason="A tool name is required.")
        if normalized_action in _PROHIBITED_ACTIONS:
            return ValidationResult(valid=False, reason=f"Tool '{normalized_action}' is blocked by safety policy.")

        action_def = registry.get_tool(normalized_action)
        if action_def is None:
            return ValidationResult(valid=False, reason=f"Unknown tool '{normalized_action}'.")

        raw_params = dict(params or {})
        sanitized: dict[str, Any] = {}
        schema = self._FIELD_SCHEMAS.get(normalized_action)
        schema_map = {field.name: field for field in tuple(schema or ())}

        for param_name, value in raw_params.items():
            if str(param_name).startswith("_"):
                sanitized[str(param_name)] = value
                continue
            if str(param_name) in _CONTROL_PARAMS:
                sanitized[str(param_name)] = self._coerce_bool(value)
                continue
            field = schema_map.get(str(param_name))
            if field is None and schema is not None:
                return ValidationResult(
                    valid=False,
                    reason=f"Unexpected argument '{param_name}' for tool '{normalized_action}'.",
                    permission_level=permission_level_for(normalized_action),
                )
            try:
                if field is None:
                    coerced = self._coerce_generic(value)
                else:
                    coerced = self._coerce_field(field, value)
            except (TypeError, ValueError) as exc:
                return ValidationResult(
                    valid=False,
                    reason=str(exc),
                    permission_level=permission_level_for(normalized_action),
                )
            sanitized[str(param_name)] = coerced

        required_fields = set(action_def.required_params)
        if schema is not None:
            required_fields.update(field.name for field in schema if field.required)
        missing = [name for name in sorted(required_fields) if name not in sanitized]
        if missing:
            return ValidationResult(
                valid=False,
                reason=f"Missing required argument(s) for '{normalized_action}': {', '.join(missing)}.",
                permission_level=permission_level_for(normalized_action),
            )

        normalized_target = self._normalized_target(normalized_action, sanitized, target)
        return ValidationResult(
            valid=True,
            sanitized_params=sanitized,
            reason="Validated.",
            normalized_target=normalized_target,
            permission_level=permission_level_for(normalized_action),
        )

    def _coerce_field(self, field: FieldSpec, value: Any) -> Any:
        if field.kind == "browser":
            browser = self._coerce_string(value, max_length=field.max_length, collapse_whitespace=True)
            browser = browser.lower()
            if browser not in BROWSER_APPS:
                raise ValueError(f"Unsupported browser '{browser}'.")
            return browser
        if field.kind == "enum":
            normalized = self._coerce_string(value, max_length=field.max_length, collapse_whitespace=True).lower()
            if normalized not in {item.lower() for item in field.allowed_values}:
                raise ValueError(f"Unsupported value '{normalized}' for '{field.name}'.")
            return normalized
        if field.kind == "identifier":
            identifier = self._coerce_string(value, max_length=field.max_length, collapse_whitespace=True).lower()
            if not _IDENTIFIER_RE.fullmatch(identifier):
                raise ValueError(f"Argument '{field.name}' contains unsafe characters.")
            return identifier
        if field.kind == "integer":
            number = self._coerce_int(value)
            self._check_numeric_range(field, number)
            return number
        if field.kind == "memory_namespace":
            namespace = self._coerce_string(value, max_length=field.max_length, collapse_whitespace=True).lower()
            if not _MEMORY_NAMESPACE_RE.fullmatch(namespace):
                raise ValueError(f"Argument '{field.name}' contains unsafe characters.")
            return namespace
        if field.kind == "path":
            candidate = self._coerce_string(value, max_length=field.max_length, collapse_whitespace=False).strip().strip('"').strip("'")
            self._ensure_no_injection(candidate, field.name, allow_newlines=False)
            return candidate
        if field.kind == "path_or_folder":
            candidate = self._coerce_string(value, max_length=field.max_length, collapse_whitespace=False).strip().strip('"').strip("'")
            if not candidate:
                return "project"
            lowered = " ".join(candidate.lower().split())
            if lowered in KNOWN_FOLDERS:
                return lowered
            self._ensure_no_injection(candidate, field.name, allow_newlines=False)
            return candidate
        if field.kind == "query":
            query = self._coerce_string(value, max_length=field.max_length, collapse_whitespace=True)
            return query
        if field.kind == "string":
            cleaned = self._coerce_string(value, max_length=field.max_length, collapse_whitespace=True)
            if not cleaned and not field.allow_empty:
                raise ValueError(f"Argument '{field.name}' must not be empty.")
            return cleaned
        if field.kind == "text":
            cleaned = self._coerce_string(value, max_length=field.max_length, collapse_whitespace=False)
            if not cleaned and not field.allow_empty:
                raise ValueError(f"Argument '{field.name}' must not be empty.")
            return cleaned
        if field.kind == "url":
            return self._coerce_url(value, field.name, allow_alias=False, max_length=field.max_length)
        if field.kind == "url_or_alias":
            return self._coerce_url(value, field.name, allow_alias=True, max_length=field.max_length)
        raise ValueError(f"Unsupported validator kind '{field.kind}'.")

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        return normalized in {"1", "true", "yes", "on"}

    @staticmethod
    def _coerce_generic(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        raise ValueError("Only scalar arguments are allowed for dynamically registered tools.")

    @staticmethod
    def _coerce_int(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("Boolean values are not valid integers.")
        if isinstance(value, int):
            return value
        normalized = str(value).strip()
        return int(normalized)

    @staticmethod
    def _check_numeric_range(field: FieldSpec, value: int | float) -> None:
        if field.min_value is not None and value < field.min_value:
            raise ValueError(f"Argument '{field.name}' must be at least {field.min_value}.")
        if field.max_value is not None and value > field.max_value:
            raise ValueError(f"Argument '{field.name}' must be at most {field.max_value}.")

    @staticmethod
    def _coerce_string(value: Any, *, max_length: int, collapse_whitespace: bool) -> str:
        if value is None:
            return ""
        text = str(value)
        if any(ord(char) < 32 and char not in {"\n", "\r", "\t"} for char in text):
            raise ValueError("Control characters are not allowed.")
        cleaned = " ".join(text.split()) if collapse_whitespace else text.strip()
        if len(cleaned) > max_length:
            raise ValueError(f"Argument is too long (max {max_length} characters).")
        return cleaned

    @staticmethod
    def _ensure_no_injection(text: str, field_name: str, *, allow_newlines: bool) -> None:
        for marker in _DANGEROUS_TEXT_MARKERS:
            if marker in text:
                if allow_newlines and marker in {"\r", "\n"}:
                    continue
                raise ValueError(f"Argument '{field_name}' contains unsafe shell control characters.")

    def _coerce_url(self, value: Any, field_name: str, *, allow_alias: bool, max_length: int) -> str:
        cleaned = self._coerce_string(value, max_length=max_length, collapse_whitespace=False).strip()
        lowered = cleaned.lower()
        if lowered.startswith(("javascript:", "data:", "file:", "vbscript:")):
            raise ValueError(f"Argument '{field_name}' uses a blocked URL scheme.")
        if not allow_alias and "://" not in lowered and lowered:
            raise ValueError(f"Argument '{field_name}' must be an explicit http(s) URL.")
        return cleaned

    @staticmethod
    def _normalized_target(action_name: str, params: dict[str, Any], target: str) -> str:
        if str(target).strip():
            return str(target).strip()
        for key in ("app_name", "query", "url", "name", "path", "message", "action_type"):
            candidate = str(params.get(key, "") or "").strip()
            if candidate:
                return candidate
        return action_name
