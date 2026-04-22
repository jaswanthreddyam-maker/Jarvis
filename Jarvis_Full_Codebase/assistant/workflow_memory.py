"""Workflow memory for successful multi-step patterns and legacy command reuse."""
from __future__ import annotations

import json
import logging
import math
import platform
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from assistant.workflow_policy import build_command_context, contexts_match

logger = logging.getLogger("Jarvis.WorkflowMemory")

_PLACEHOLDER_RE = re.compile(r"<([a-z_]+)>")
_YOUTUBE_SEARCH_PATTERNS = (
    re.compile(r"^search\s+(.+?)\s+(?:on|in)\s+youtube$", re.IGNORECASE),
    re.compile(r"^search\s+(.+?)\s+youtube$", re.IGNORECASE),
    re.compile(r"^youtube\s+lo\s+(.+?)\s+search$", re.IGNORECASE),
    re.compile(r"^youtube\s+(.+?)\s+search$", re.IGNORECASE),
)
_RETENTION_USE_WEIGHT = 0.7
_RETENTION_RECENCY_WEIGHT = 0.3
_RECENCY_WINDOW_SECONDS = 7 * 24 * 60 * 60
_REUSE_CONFIDENCE_THRESHOLD = 0.6
_REUSE_MATCH_THRESHOLD = 0.75
_REFERENTIAL_TERMS = {
    "it",
    "that",
    "this",
    "them",
    "those",
    "these",
    "he",
    "she",
    "him",
    "her",
    "there",
}


class WorkflowMemory:
    def __init__(
        self,
        storage_dir: Path | str | None = None,
        *,
        max_patterns: int = 24,
        max_legacy_workflows: int = 24,
    ) -> None:
        if storage_dir is None:
            storage_path = Path(__file__).resolve().parents[1] / "memory" / "workflows.json"
        else:
            candidate = Path(storage_dir)
            storage_path = candidate if candidate.suffix.lower() == ".json" else candidate / "workflows.json"

        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._legacy_workflows: dict[str, dict[str, Any]] = {}
        self._patterns: list[dict[str, Any]] = []
        self._max_patterns = max(1, max_patterns)
        self._max_legacy_workflows = max(1, max_legacy_workflows)
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self._storage_path.exists():
            return

        try:
            with open(self._storage_path, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
        except Exception as exc:
            logger.error("Failed to load workflows: %s", exc)
            return

        if isinstance(payload, dict) and "legacy_tasks" in payload:
            self._legacy_workflows = dict(payload.get("legacy_tasks", {}))
            self._patterns = list(payload.get("patterns", []))
            return

        if isinstance(payload, dict):
            # Backward compatibility with the old flat storage shape.
            self._legacy_workflows = dict(payload)
            self._patterns = []
            logger.info("Loaded legacy workflow memory with %d tasks", len(self._legacy_workflows))

    def _save(self) -> None:
        payload = {
            "legacy_tasks": self._legacy_workflows,
            "patterns": self._patterns,
        }
        try:
            with open(self._storage_path, "w", encoding="utf-8") as file_obj:
                json.dump(payload, file_obj, indent=2)
        except Exception as exc:
            logger.error("Failed to save workflows: %s", exc)

    def save_workflow(self, task_name: str, os_name: str, steps: list[str]) -> None:
        """Legacy workflow storage kept for compatibility with the old executor."""
        with self._lock:
            normalized_task = self._normalize_text(task_name).replace(" ", "_")
            self._legacy_workflows[normalized_task] = {
                "task": normalized_task,
                "os": os_name,
                "os_release": platform.release(),
                "steps": steps,
                "success": True,
                "created_at": time.time(),
                "last_used_at": time.time(),
                "use_count": 1,
            }
            self._prune_legacy_workflows_locked()
            self._save()
            logger.info("Saved legacy workflow: %s", normalized_task)

    def get_workflow(self, task_name: str, os_name: str) -> dict[str, Any] | None:
        with self._lock:
            normalized_task = self._normalize_text(task_name).replace(" ", "_")
            workflow = self._legacy_workflows.get(normalized_task)

            if not workflow or not workflow.get("success"):
                return None
            if workflow.get("os") != os_name:
                return None

            saved_release = workflow.get("os_release")
            current_release = platform.release()
            if saved_release and current_release and saved_release != current_release:
                return None
            workflow["last_used_at"] = time.time()
            workflow["use_count"] = int(workflow.get("use_count", 1)) + 1
            self._legacy_workflows[normalized_task] = workflow
            self._save()
            return deepcopy(workflow)

    def remember_successful_workflow(
        self,
        goal: str,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Learn a reusable pattern from a successful multi-step workflow."""
        return self._remember_workflow_outcome(goal, steps, success=True)

    def remember_failed_workflow(
        self,
        goal: str,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Track a failed workflow so unstable patterns lose reuse priority."""
        return self._remember_workflow_outcome(goal, steps, success=False)

    def _remember_workflow_outcome(
        self,
        goal: str,
        steps: list[dict[str, Any]],
        *,
        success: bool,
    ) -> dict[str, Any] | None:
        pattern_record = self._build_pattern_record(goal, steps)
        if pattern_record is None:
            return None

        with self._lock:
            existing_index = next(
                (
                    index
                    for index, candidate in enumerate(self._patterns)
                    if self._same_pattern(candidate, pattern_record)
                ),
                None,
            )
            if existing_index is None:
                self._apply_outcome(pattern_record, success=success)
                self._patterns.append(pattern_record)
            else:
                previous = self._patterns[existing_index]
                merged = self._merge_pattern_record(previous, pattern_record)
                self._apply_outcome(merged, success=success)
                self._patterns[existing_index] = merged
            self._prune_patterns_locked()
            self._save()
        outcome = "successful" if success else "failed"
        logger.info("Recorded %s workflow pattern: %s", outcome, pattern_record["pattern"])
        if existing_index is None:
            return deepcopy(pattern_record)
        return deepcopy(self._patterns[existing_index])

    def find_similar_workflow(self, user_input: str) -> dict[str, Any] | None:
        normalized = self._normalize_text(user_input)
        if not normalized:
            return None

        request_analysis = self._analyze_command(user_input)
        request_context = build_command_context(user_input)
        best_match: dict[str, Any] | None = None
        best_index: int | None = None
        best_score = 0.0
        with self._lock:
            candidates = deepcopy(self._patterns)

        for index, candidate in enumerate(candidates):
            if not contexts_match(candidate.get("context"), request_context):
                continue
            confidence = self._pattern_confidence(candidate)
            if confidence < _REUSE_CONFIDENCE_THRESHOLD:
                continue
            extracted_slots = self._match_pattern(candidate, normalized)
            if extracted_slots is None:
                continue

            slot_semantics = self._build_slot_semantics(extracted_slots, request_analysis)
            if any(meta.get("is_reference") for meta in slot_semantics.values()):
                continue

            score = self._score_match(candidate, normalized, extracted_slots, request_analysis)
            if score <= best_score:
                continue

            best_score = score
            best_index = index
            try:
                instantiated_steps = self._instantiate_steps(candidate["steps"], extracted_slots)
            except ValueError as exc:
                logger.warning("Skipping workflow pattern '%s' due to invalid slot replacement: %s", candidate.get("pattern", ""), exc)
                continue
            best_match = {
                "pattern": candidate["pattern"],
                "steps": instantiated_steps,
                "slots": extracted_slots,
                "slot_semantics": slot_semantics,
                "intent_nuance": deepcopy(candidate.get("intent_nuance", {})),
                "confidence": confidence,
                "score": score,
                "reused_from_memory": True,
            }

        if best_score < _REUSE_MATCH_THRESHOLD:
            return None

        assert best_match is not None, "Workflow score threshold met without a resolved match."
        assert best_index is not None, "Workflow score threshold met without a pattern index."
        tracking_updated = self._record_pattern_use(best_match["pattern"])
        best_match["usage_tracking_updated"] = tracking_updated
        if not tracking_updated:
            logger.warning("Workflow match for '%s' was returned without usage tracking persistence.", best_match["pattern"])
        return best_match

    @staticmethod
    def _normalize_text(text: str) -> str:
        cleaned = re.sub(r"[?!,]+", " ", text.strip().lower())
        cleaned = " ".join(cleaned.split())
        return WorkflowMemory._analyze_command(cleaned)["match_text"]

    @staticmethod
    def _analyze_command(text: str) -> dict[str, Any]:
        normalized = " ".join(text.strip().lower().split())
        for pattern in _YOUTUBE_SEARCH_PATTERNS:
            match = pattern.fullmatch(normalized)
            if match is None:
                continue
            query = " ".join(match.group(1).strip().split())
            return {
                "normalized_text": normalized,
                "match_text": f"open youtube and search {query}",
                "intent_nuance": {
                    "intent_meaning": "youtube_search",
                    "platform": "youtube",
                    "open_explicit": False,
                    "search_explicit": True,
                    "expression_style": "implicit_platform_search" if normalized.startswith("search ") else "platform_first_search",
                },
                "slot_semantics": {
                    "query": WorkflowMemory._query_semantics(query, intent_meaning="youtube_search"),
                },
            }
        return {
            "normalized_text": normalized,
            "match_text": normalized,
            "intent_nuance": {
                "intent_meaning": "direct_command",
                "platform": "",
                "open_explicit": normalized.startswith("open "),
                "search_explicit": normalized.startswith("search "),
                "expression_style": "explicit_open" if normalized.startswith("open ") else ("explicit_search" if normalized.startswith("search ") else "direct"),
            },
            "slot_semantics": {},
        }

    def _build_pattern_record(
        self,
        goal: str,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        command_analysis = self._analyze_command(goal)
        normalized_goal = str(command_analysis["match_text"])
        if len(steps) < 2:
            return None

        first = steps[0]
        second = steps[1]
        second_params = dict(second.get("params", {}))
        query = str(second_params.get("query") or second.get("target") or "").strip()
        if not query:
            return None

        first_surface = self._surface_for_step(first)
        second_action = str(second.get("intent") or second.get("action") or "").strip().lower()
        if first_surface == "youtube" and second_action == "search_youtube":
            pattern = "open youtube and search <query>"
        elif first_surface and second_action in {"search_web", "search_youtube"}:
            pattern = f"open {first_surface} and search <query>"
        else:
            return None

        template_steps: list[dict[str, Any]] = []
        for index, raw_step in enumerate(steps, start=1):
            template_step = {
                "step_id": index,
                "action": str(raw_step.get("intent") or raw_step.get("action") or "").strip(),
                "target": str(raw_step.get("target", "")).strip(),
                "params": deepcopy(raw_step.get("params", {})),
            }
            if index == 2:
                template_step["target"] = "<query>"
                template_step["params"]["query"] = "<query>"
                template_step["depends_on"] = [1]
            elif index > 1:
                template_step["depends_on"] = [index - 1]
            template_steps.append(template_step)

        return {
            "pattern": pattern,
            "normalized_goal": normalized_goal,
            "context": build_command_context(goal, steps),
            "intent_nuance": deepcopy(command_analysis.get("intent_nuance", {})),
            "slot_semantics": self._build_slot_semantics({"query": query}, command_analysis),
            "steps": template_steps,
            "created_at": time.time(),
            "last_used_at": time.time(),
            "successful_runs": 0,
            "failed_runs": 0,
            "use_count": 0,
            "confidence": 0.0,
        }

    @staticmethod
    def _surface_for_step(step: dict[str, Any]) -> str:
        action = str(step.get("intent") or step.get("action") or "").strip().lower()
        target = str(step.get("target", "")).strip().lower()
        params = dict(step.get("params", {}))
        url = str(params.get("url", "")).strip().lower()
        app_name = str(params.get("app_name", "")).strip().lower()

        if action == "open_app" and app_name:
            return app_name
        if action in {"open_url", "search_youtube"}:
            if "youtube" in target or "youtube.com" in url or "youtu.be" in url:
                return "youtube"
        if action == "open_url" and target:
            return target
        return app_name or target

    def _match_pattern(self, candidate: dict[str, Any], normalized_input: str) -> dict[str, str] | None:
        pattern = str(candidate.get("pattern", "")).strip().lower()
        if not pattern:
            return None

        regex = self._pattern_regex(pattern)
        match = regex.fullmatch(normalized_input)
        if match is None:
            return None
        slots = {key: " ".join(value.strip().split()) for key, value in match.groupdict().items()}
        if not self._slots_are_valid(slots):
            return None
        return slots

    @staticmethod
    def _score_match(
        candidate: dict[str, Any],
        normalized_input: str,
        slots: dict[str, str],
        request_analysis: dict[str, Any],
    ) -> float:
        pattern = str(candidate.get("pattern", "")).strip().lower()
        literal_tokens = set(re.findall(r"[a-z0-9]+", _PLACEHOLDER_RE.sub(" ", pattern)))
        input_tokens = set(re.findall(r"[a-z0-9]+", normalized_input))
        union = literal_tokens | input_tokens
        overlap = len(literal_tokens & input_tokens) / len(union) if union else 0.0
        slot_bonus = 0.1 if all(value for value in slots.values()) else 0.0
        candidate_nuance = dict(candidate.get("intent_nuance", {}))
        request_nuance = dict(request_analysis.get("intent_nuance", {}))
        nuance_bonus = 0.0
        if candidate_nuance.get("intent_meaning") == request_nuance.get("intent_meaning"):
            nuance_bonus += 0.08
        if bool(candidate_nuance.get("open_explicit")) == bool(request_nuance.get("open_explicit")):
            nuance_bonus += 0.04
        if bool(candidate_nuance.get("search_explicit")) == bool(request_nuance.get("search_explicit")):
            nuance_bonus += 0.03
        return overlap + slot_bonus + nuance_bonus

    @staticmethod
    def _pattern_regex(pattern: str) -> re.Pattern[str]:
        pieces: list[str] = []
        cursor = 0
        for match in _PLACEHOLDER_RE.finditer(pattern):
            literal = re.escape(pattern[cursor:match.start()]).replace(r"\ ", r"\s+")
            pieces.append(literal)
            pieces.append(fr"(?P<{match.group(1)}>.+)")
            cursor = match.end()
        pieces.append(re.escape(pattern[cursor:]).replace(r"\ ", r"\s+"))
        return re.compile(r"^" + "".join(pieces) + r"$", re.IGNORECASE)

    def _instantiate_steps(
        self,
        steps: list[dict[str, Any]],
        slots: dict[str, str],
    ) -> list[dict[str, Any]]:
        instantiated = [self._replace_placeholders(step, slots) for step in steps]
        if any(self._contains_unresolved_placeholder(step) for step in instantiated):
            raise ValueError("Unresolved workflow placeholder remained after slot replacement.")
        return instantiated

    def _replace_placeholders(self, value: Any, slots: dict[str, str]) -> Any:
        if isinstance(value, dict):
            return {key: self._replace_placeholders(item, slots) for key, item in value.items()}
        if isinstance(value, list):
            return [self._replace_placeholders(item, slots) for item in value]
        if isinstance(value, str):
            replaced = value
            for slot_name, slot_value in slots.items():
                if not slot_value.strip():
                    raise ValueError(f"Slot '{slot_name}' resolved to an empty value.")
                replaced = replaced.replace(f"<{slot_name}>", slot_value)
            return replaced
        return value

    @staticmethod
    def _slots_are_valid(slots: dict[str, str]) -> bool:
        if not slots:
            return True
        return all(value.strip() and not _PLACEHOLDER_RE.search(value) for value in slots.values())

    def _contains_unresolved_placeholder(self, value: Any) -> bool:
        if isinstance(value, dict):
            return any(self._contains_unresolved_placeholder(item) for item in value.values())
        if isinstance(value, list):
            return any(self._contains_unresolved_placeholder(item) for item in value)
        if isinstance(value, str):
            return _PLACEHOLDER_RE.search(value) is not None
        return False

    def _record_pattern_use(self, pattern_name: str) -> bool:
        with self._lock:
            pattern_index = next(
                (i for i, p in enumerate(self._patterns) if p.get("pattern") == pattern_name),
                None
            )
            if pattern_index is None:
                logger.warning("Cannot update workflow usage for '%s': pattern not found.", pattern_name)
                return False

            self._patterns[pattern_index]["last_used_at"] = time.time()
            self._patterns[pattern_index]["use_count"] = int(self._patterns[pattern_index].get("use_count", 1)) + 1
            self._patterns[pattern_index]["confidence"] = self._pattern_confidence(self._patterns[pattern_index])
            self._save()
            return True

    def _retention_score(self, record: dict[str, Any], *, now: float) -> float:
        use_count = max(0, int(record.get("use_count", record.get("successful_runs", 0))))
        last_used_at = float(record.get("last_used_at", record.get("created_at", now)))
        age_seconds = max(0.0, now - last_used_at)
        use_score = math.log1p(use_count)
        recency_score = 1.0 / (1.0 + math.log1p(age_seconds / _RECENCY_WINDOW_SECONDS))
        confidence_score = self._pattern_confidence(record)
        return (_RETENTION_USE_WEIGHT * use_score) + (_RETENTION_RECENCY_WEIGHT * recency_score) + (0.2 * confidence_score)

    def _pattern_confidence(self, record: dict[str, Any]) -> float:
        successful_runs = max(0, int(record.get("successful_runs", 0)))
        failed_runs = max(0, int(record.get("failed_runs", 0)))
        total = successful_runs + failed_runs
        base_confidence = (successful_runs + 1.0) / (total + 2.0)
        instability_penalty = failed_runs / max(1.0, total)
        return max(0.0, min(1.0, base_confidence * (1.0 - (0.65 * instability_penalty))))

    @staticmethod
    def _query_semantics(query: str, *, intent_meaning: str) -> dict[str, Any]:
        normalized = " ".join(query.strip().lower().split())
        is_reference = normalized in _REFERENTIAL_TERMS
        token_count = len(re.findall(r"[a-z0-9]+", normalized))
        query_type = "referential" if is_reference else ("keyword_phrase" if token_count <= 4 else "descriptive_phrase")
        return {
            "name": "query",
            "value": normalized,
            "query_type": query_type,
            "intent_meaning": intent_meaning,
            "is_reference": is_reference,
        }

    def _build_slot_semantics(
        self,
        slots: dict[str, str],
        command_analysis: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        existing = dict(command_analysis.get("slot_semantics", {}))
        semantics: dict[str, dict[str, Any]] = {}
        for slot_name, slot_value in slots.items():
            if slot_name == "query":
                semantics[slot_name] = self._query_semantics(
                    slot_value,
                    intent_meaning=str(command_analysis.get("intent_nuance", {}).get("intent_meaning", "search")),
                )
                continue
            current = dict(existing.get(slot_name, {}))
            current["name"] = slot_name
            current["value"] = slot_value.strip().lower()
            current.setdefault("intent_meaning", str(command_analysis.get("intent_nuance", {}).get("intent_meaning", "parameter")))
            current.setdefault("is_reference", current.get("value") in _REFERENTIAL_TERMS)
            semantics[slot_name] = current
        return semantics

    @staticmethod
    def _same_pattern(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return (
            left.get("pattern") == right.get("pattern")
            and left.get("context") == right.get("context")
            and left.get("intent_nuance", {}).get("intent_meaning") == right.get("intent_nuance", {}).get("intent_meaning")
        )

    def _merge_pattern_record(self, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        merged = deepcopy(current)
        merged["created_at"] = float(previous.get("created_at", current.get("created_at", time.time())))
        merged["last_used_at"] = float(previous.get("last_used_at", current.get("last_used_at", time.time())))
        merged["successful_runs"] = int(previous.get("successful_runs", 0))
        merged["failed_runs"] = int(previous.get("failed_runs", 0))
        merged["use_count"] = int(previous.get("use_count", 0))
        merged["confidence"] = float(previous.get("confidence", self._pattern_confidence(previous)))
        merged["slot_semantics"] = deepcopy(previous.get("slot_semantics") or current.get("slot_semantics", {}))
        return merged

    def _apply_outcome(self, record: dict[str, Any], *, success: bool) -> None:
        if success:
            record["successful_runs"] = int(record.get("successful_runs", 0)) + 1
        else:
            record["failed_runs"] = int(record.get("failed_runs", 0)) + 1
        record["confidence"] = self._pattern_confidence(record)

    def _prune_patterns_locked(self) -> None:
        if len(self._patterns) <= self._max_patterns:
            return
        now = time.time()
        self._patterns.sort(key=lambda item: self._retention_score(item, now=now))
        self._patterns = self._patterns[-self._max_patterns :]

    def _prune_legacy_workflows_locked(self) -> None:
        if len(self._legacy_workflows) <= self._max_legacy_workflows:
            return

        now = time.time()
        ranked = sorted(
            self._legacy_workflows.items(),
            key=lambda item: self._retention_score(item[1], now=now),
        )
        excess = len(self._legacy_workflows) - self._max_legacy_workflows
        for key, _ in ranked[:excess]:
            self._legacy_workflows.pop(key, None)


workflow_memory = WorkflowMemory()
