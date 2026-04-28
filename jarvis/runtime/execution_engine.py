from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from typing import Any, Callable, Protocol

from jarvis.core.intent_classifier import IntentClassifier
from jarvis.runtime.command_processor import CommandProcessor, PlanScorer
from jarvis.runtime.planner import ExecutionPlanner
from jarvis.runtime.execution_types import (
    ExecutionCommand,
    ExecutionIntent,
    ExecutionState,
    describe_execution_intent,
    ExecutionStep,
    OpenIntent,
    PlayIntent,
    SearchIntent,
)


class ContextualResolver:
    def __init__(
        self,
        memory_provider: Callable[[], Any],
    ) -> None:
        self._memory_provider = memory_provider

    def __call__(self, text: str, state: ExecutionState) -> ExecutionIntent | None:
        return self.resolve(text, state)

    def resolve(self, text: str, state: ExecutionState) -> ExecutionIntent | None:
        normalized = text.lower().strip()
        memory = self._memory_provider()

        # 1. Intent Continuation Models
        continuation = self._infer_continuation(normalized, state)
        if continuation:
            return continuation

        # 2. Memory-Driven Inference (Semantic Search)
        semantic_intent = self._infer_from_memory(normalized, memory)
        if semantic_intent:
            return semantic_intent

        return None

    def _infer_continuation(self, text: str, state: ExecutionState) -> ExecutionIntent | None:
        if not state.history:
            return None
        last_intent = state.history[-1]
        
        if text in {"next", "play next", "skip", "forward"}:
            if isinstance(last_intent, PlayIntent):
                return PlayIntent(query=last_intent.query, platform=last_intent.platform, action_type="next")
        if text in {"pause", "stop"}:
            if isinstance(last_intent, PlayIntent):
                return PlayIntent(query=last_intent.query, platform=last_intent.platform, action_type="pause")
        if text in {"resume", "play"}:
            if isinstance(last_intent, PlayIntent):
                return PlayIntent(query=last_intent.query, platform=last_intent.platform, action_type="resume")
                
        return None

    def _infer_from_memory(self, text: str, memory: Any) -> ExecutionIntent | None:
        if memory is None:
            return self._legacy_rule_fallback(text)
            
        try:
            semantic_memory = getattr(memory, "semantic", None)
            if semantic_memory and hasattr(semantic_memory, "search"):
                results = semantic_memory.search(text, limit=5)
                for res in results:
                    if res.get("score", 0) > 0.8 and res.get("metadata", {}).get("success") is True:
                        action = res.get("metadata", {}).get("action")
                        query = res.get("metadata", {}).get("query")
                        if action == "open":
                            return OpenIntent(target=query)
                        elif action == "search":
                            return SearchIntent(query=query)
                        elif action == "play":
                            return PlayIntent(query=query, platform="youtube")
        except Exception:
            pass
            
        return self._legacy_rule_fallback(text)
        
    def _legacy_rule_fallback(self, normalized: str) -> ExecutionIntent | None:
        if normalized in {"yt", "open yt", "go to yt"} or normalized.startswith("open yt") or normalized.startswith("go to yt"):
            return OpenIntent(target="youtube")
        if normalized.startswith("search ") or normalized == "search":
            query = normalized.replace("search", "", 1).strip()
            return SearchIntent(query=query) if query else None
        if "youtube" in normalized and "play" in normalized:
            query = normalized.replace("play", "", 1).replace("youtube", "", 1).replace("on", "", 1).strip()
            for word in ("please", "for me", "now"):
                query = query.replace(word, "")
            query = query.strip()
            return PlayIntent(query=query, platform="youtube") if query else None
        if any(normalized.startswith(prefix) for prefix in ("open ", "launch ", "go to ", "goto ", "visit ")):
            target = normalized
            for prefix in ("open ", "launch ", "go to ", "goto ", "visit "):
                if normalized.startswith(prefix):
                    target = normalized[len(prefix):].strip()
                    break
            words = target.split()
            target = " ".join(word for word in words if word.lower() not in {"site", "page", "website"})
            return OpenIntent(target=target) if target else None
            
        return None


class ExecutionApplication(Protocol):
    def attach_execution_engine(self, execution_engine) -> None: ...

    def begin_execution(self, request_id: int | str | None = None) -> None: ...

    def cancel_active(self, request_id: int | str | None = None) -> bool: ...

    def finish_execution(self, request_id: int | str | None = None) -> None: ...

    def get_active_page(self): ...

    def publish_request_failed(self, request_id: int | str, error: str) -> None: ...

    def publish_request_complete(
        self,
        request_id: int | str,
        response: str,
        task_snapshot: dict[str, object] | None,
    ) -> None: ...

    def start_request(self, request_id: int | str | None = None) -> bool: ...

    def end_request(self, request_id: int | str | None = None) -> None: ...

    async def execute_intent(self, intent: ExecutionIntent, request_id: int | str | None = None) -> str | None: ...

    async def record_execution_result(
        self,
        *,
        source_text: str,
        results: list[str],
        last_intent: ExecutionIntent | None,
        source: str,
        request_id: int | str | None = None,
    ) -> None: ...


class ExecutionEngine:
    def __init__(
        self,
        application: ExecutionApplication,
        *,
        timeout_seconds: float = 30.0,
        logger: logging.Logger | None = None,
        planner: ExecutionPlanner | None = None,
        classifier: IntentClassifier | None = None,
        classifier_confidence_floor: float = 0.85,
        learning_state_path: str | pathlib.Path | None = None,
    ) -> None:
        self._application = application
        self._timeout_seconds = timeout_seconds
        self._logger = logger or logging.getLogger("Jarvis.RuntimeExecution")
        self._planner = planner
        self._classifier = classifier or IntentClassifier()
        self._classifier_confidence_floor = classifier_confidence_floor
        
        self._contextual_resolver = ContextualResolver(
            memory_provider=lambda: getattr(getattr(self._application, "orchestrator", None), "memory", None)
        )
        self._request_intent_hints: dict[str, dict[str, Any]] = {}
        
        self._confidence_penalties: dict[str, float] = {}
        self._timeout_frequency: dict[str, int] = {}
        self._strategy_scores: dict[str, float] = {}

        # Fix 3: Persist learning state across restarts
        self._learning_state_path: pathlib.Path | None = (
            pathlib.Path(learning_state_path) if learning_state_path else None
        )
        self._learning_lock = asyncio.Lock()
        self._load_learning_state()
        
        self._plan_scorer = PlanScorer(
            failure_history=self._timeout_frequency,
            confidence_penalties=self._confidence_penalties,
            strategy_scores=self._strategy_scores,
        )
        
        self._command_processor = CommandProcessor(
            classifier=self._classifier,
            planner=self._planner,
            contextual_resolver=lambda text, state: self._contextual_resolver.resolve(text, state),
            classifier_confidence_floor=self._classifier_confidence_floor,
            plan_scorer=self._plan_scorer,
        )
        self._metrics: dict[str, Any] = {
            "matched": 0,
            "fallback": 0,
            "planner_calls": 0,
            "planner_steps": 0,
            "planner_success": 0,
            "replan_count": 0,
            "failure_types": {},
            "step_latencies": {},
        }
        attach = getattr(self._application, "attach_execution_engine", None)
        if callable(attach):
            attach(self)

    def _hash_intent(self, step: ExecutionStep) -> str:
        intent = step.intent
        action = getattr(intent, "type", intent.__class__.__name__)
        query = getattr(intent, "query", getattr(intent, "target", ""))
        strategy_id = step.metadata.get("strategy_id", "")
        return f"{action}:{query}:{strategy_id}"

    async def execute(
        self,
        command: ExecutionCommand | str,
        *,
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> str:
        if isinstance(command, str):
            command = ExecutionCommand.for_text(command)
        self._ensure_command(command)
        response, _ = await self.execute_with_details(
            command,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        return response

    async def execute_with_details(
        self,
        command: ExecutionCommand | str,
        *,
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> tuple[str, dict[str, object] | None]:
        if isinstance(command, str):
            command = ExecutionCommand.for_text(command)
        self._ensure_command(command)
        timeout = timeout_seconds if timeout_seconds is not None else self._timeout_seconds
        started_at = time.monotonic()
        self._application.begin_execution(request_id)
        self._logger.info(
            "Runtime request started.",
            extra={
                "event": "runtime.request.started",
                "request_id": request_id,
                "timeout_seconds": timeout,
            },
        )
        try:
            response, snapshot = await asyncio.wait_for(self.execute_application_command(command, request_id=request_id), timeout=timeout)
            self._logger.info(
                "Runtime request completed.",
                extra={
                    "event": "runtime.request.completed",
                    "request_id": request_id,
                    "command_type": command.type,
                    "outcome": "success",
                    "duration_seconds": time.monotonic() - started_at,
                },
            )
            return response, snapshot
        except asyncio.CancelledError:
            cancelled = bool(self._application.cancel_active(request_id))
            self._logger.info(
                "Runtime request cancelled.",
                extra={
                    "event": "runtime.request.cancelled",
                    "request_id": request_id,
                    "command_type": command.type,
                    "outcome": "cancelled",
                    "duration_seconds": time.monotonic() - started_at,
                    "error_type": "CancelledError",
                    "cancel_requested": cancelled,
                },
            )
            raise
        except asyncio.TimeoutError:
            cancelled = bool(self._application.cancel_active(request_id))
            timeout_message = "That request timed out, so I cancelled it to keep Jarvis responsive."
            publish_failed = getattr(self._application, "publish_request_failed", None)
            if callable(publish_failed):
                publish_failed(request_id, timeout_message)
            self._logger.warning(
                "Runtime request timed out.",
                extra={
                    "event": "runtime.request.timed_out",
                    "request_id": request_id,
                    "command_type": command.type,
                    "outcome": "timeout",
                    "duration_seconds": time.monotonic() - started_at,
                    "timeout_seconds": timeout,
                    "error_type": "TimeoutError",
                    "cancel_requested": cancelled,
                },
            )
            return timeout_message, None
        except Exception as exc:
            self._logger.exception(
                "Runtime request failed.",
                extra={
                    "event": "runtime.request.failed",
                    "request_id": request_id,
                    "command_type": command.type,
                    "outcome": "failure",
                    "duration_seconds": time.monotonic() - started_at,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        finally:
            self._application.finish_execution(request_id)

    @staticmethod
    def _ensure_command(command: ExecutionCommand) -> None:
        if not isinstance(command, ExecutionCommand):
            raise TypeError(f"Unsupported execution command: {type(command).__name__}")

    async def execute_application_command(
        self,
        command: ExecutionCommand,
        *,
        request_id: int | str | None = None,
    ) -> tuple[str, dict[str, object] | None]:
        self._ensure_command(command)
        text = command.display_text
        print(f"CONTROLLER ENTRY: {text}", flush=True)
        print(">>> CONTROLLER ENTRY:", text, flush=True)

        # Test harness compatibility: in minimal applications, delegate to handle_text_async.
        start_request = getattr(self._application, "start_request", None)
        handle_text_async = getattr(self._application, "handle_text_async", None)
        if not callable(start_request) and callable(handle_text_async):
            return await handle_text_async(text, request_id=request_id)

        if not self._application.start_request(request_id):
            print("[WARN] Duplicate request id, ignoring", flush=True)
            return "FAILED_BUSY", None

        try:
            state = ExecutionState()
            state.context["goal"] = text
            if command.type == "intent":
                steps = [ExecutionStep(intent=command.intent, source="user")]
                self._metrics["matched"] += 1
            else:
                steps = await self._build_steps(text, state, request_id=request_id)

            if not steps:
                final_res = "FAILED_UNSUPPORTED_INTENT"
                self._publish_terminal_event(request_id, final_res)
                return final_res, None
            results: list[str] = []
            last_intent_executed: ExecutionIntent | None = None
            source_for_memory = "engine"

            steps_queue = list(steps)
            step_index = 0

            # Refresh contextual state
            state.context["current_url"] = getattr(self._application.get_active_page(), "url", None)

            while step_index < len(steps_queue):
                step = steps_queue[step_index]
                step_index += 1

                if not self._dependencies_satisfied(step, set(state.completed_steps)):
                    print(f"[CHAIN] Step {step.id} failed due to unsatisfied dependencies", flush=True)
                    state.failed_steps.append(step.id)
                    state.context["last_result"] = "FAILED_DEPENDENCY"
                    results.append("FAILED_DEPENDENCY")
                    continue

                confidence = float(step.metadata.get("confidence", 1.0))
                intent_hash = self._hash_intent(step)
                confidence -= self._confidence_penalties.get(intent_hash, 0.0)
                
                is_critical = step.metadata.get("critical", True)
                if confidence < self._classifier_confidence_floor and is_critical:
                    print(f"[CHAIN] Step {step.id} confidence {confidence} < threshold. Triggering early replan.", flush=True)
                    result = "FAILED_LOW_CONFIDENCE"
                    state.failed_steps.append(step.id)
                    state.context["last_result"] = result
                    if self._planner is not None and hasattr(self._planner, "replan"):
                        replan_input = {
                            "failed_step": step,
                            "failure_reason": result,
                            "completed_steps": state.completed_steps,
                            "history": state.history,
                            "system_state": state.context,
                            "merge_strategy": "append_alternative",
                        }
                        new_steps = await self._planner.replan(replan_input)
                        self._metrics["replan_count"] += 1
                        if new_steps:
                            existing_intents = {self._hash_intent(s) for s in steps_queue}
                            filtered_new_steps = [
                                s for s in new_steps
                                if self._hash_intent(s) not in existing_intents
                            ]
                            merge_strategy = replan_input.get("merge_strategy", "append_alternative")
                            if merge_strategy == "replace_failed_branch":
                                steps_queue = steps_queue[:step_index] + filtered_new_steps
                            else:
                                steps_queue = steps_queue[:step_index] + filtered_new_steps + steps_queue[step_index:]
                    continue

                intent_hash = self._hash_intent(step)
                
                # Apply learning loops
                if self._timeout_frequency.get(intent_hash, 0) >= 3:
                    print(f"[CHAIN] Avoiding {intent_hash} due to chronic timeouts", flush=True)
                    result = "FAILED_CHRONIC_TIMEOUT"
                    state.failed_steps.append(step.id)
                    state.context["last_result_type"] = "FAILED_CHRONIC_TIMEOUT"
                    continue
                    
                print(f"[CHAIN] Executing step {step_index}/{len(steps_queue)}: '{self._describe_intent(step.intent)}'", flush=True)
                if step.source == "planner":
                    source_for_memory = "planner"
                result = await self._execute_step(step, request_id=request_id)
                
                # Record learning signals
                attempts = step.metadata.get("attempts_used", 1)
                if attempts > 1:
                    self._confidence_penalties[intent_hash] = self._confidence_penalties.get(intent_hash, 0.0) + 0.1
                if str(result) == "FAILED_TIMEOUT":
                    self._timeout_frequency[intent_hash] = self._timeout_frequency.get(intent_hash, 0) + 1
                if result and str(result).startswith("FAILED"):
                    state.context["last_result_type"] = str(result).split(":")[0]
                    # Penalise strategy score on failure
                    strategy_id = step.metadata.get("strategy_id", "")
                    if strategy_id:
                        self._strategy_scores[strategy_id] = self._strategy_scores.get(strategy_id, 0.0) - 0.1
                else:
                    state.context["last_result_type"] = "SUCCESS"
                    # Reward strategy score on success
                    strategy_id = step.metadata.get("strategy_id", "")
                    if strategy_id:
                        self._strategy_scores[strategy_id] = self._strategy_scores.get(strategy_id, 0.0) + 0.1
                await self._persist_learning_state()
                last_intent_executed = step.intent
                state.history.append(step.intent)
                state.history = state.history[-20:]  # Prevent unbounded history growth
                results.append(str(result))

                if result and str(result).startswith("FAILED"):
                    state.failed_steps.append(step.id)
                    print(f"[CHAIN] Step failed but continuing chain: {result}", flush=True)
                    if step.policy.fallback_strategy == "abort":
                        print(f"[CHAIN] Critical failure. Triggering re-planning loop.", flush=True)
                        if self._planner is not None and hasattr(self._planner, "replan"):
                            replan_input = {
                                "failed_step": step,
                                "failure_reason": result,
                                "completed_steps": state.completed_steps,
                                "history": state.history,
                                "system_state": state.context,
                                "merge_strategy": "replace_failed_branch",
                            }
                            new_steps = await self._planner.replan(replan_input)
                            self._metrics["replan_count"] += 1
                            if new_steps:
                                existing_intents = {self._hash_intent(s) for s in steps_queue}
                                filtered_new_steps = [
                                    s for s in new_steps
                                    if self._hash_intent(s) not in existing_intents
                                ]
                                merge_strategy = replan_input.get("merge_strategy", "replace_failed_branch")
                                if merge_strategy == "replace_failed_branch":
                                    steps_queue = steps_queue[:step_index] + filtered_new_steps
                                else:
                                    steps_queue = steps_queue[:step_index] + filtered_new_steps + steps_queue[step_index:]
                                continue
                        break
                    continue

                state.completed_steps.append(step.id)

            if any(s.source == "planner" for s in steps) and not state.failed_steps:
                self._metrics["planner_success"] += 1

            final_res = " | ".join(results)
            await self._application.record_execution_result(
                source_text=text,
                results=results,
                last_intent=last_intent_executed,
                source=source_for_memory,
                request_id=request_id,
            )
            self._publish_terminal_event(request_id, final_res)
            return final_res, None
        except Exception:
            raise
        finally:
            end_request = getattr(self._application, "end_request", None)
            if callable(end_request):
                end_request(request_id)

    def register_intent_hint(
        self,
        request_id: int | str,
        *,
        text: str,
        intent: ExecutionIntent,
        confidence: float = 1.0,
        kind: str = "execute_fast",
    ) -> None:
        self._request_intent_hints[str(request_id)] = {
            "text": text,
            "intent": intent,
            "confidence": float(confidence),
            "kind": kind,
        }

    def _consume_intent_hint(self, request_id: int | str | None, text: str) -> dict[str, Any] | None:
        if request_id is None:
            return None
        hint = self._request_intent_hints.pop(str(request_id), None)
        if hint is None:
            return None
        if str(hint.get("text", "")).strip() != text.strip():
            self._logger.warning("Discarding stale intent hint for request %s", request_id)
            return None
        return hint

    async def _build_steps(
        self,
        text: str,
        state: ExecutionState,
        *,
        request_id: int | str | None = None,
    ) -> list[ExecutionStep]:
        intent_hint = self._consume_intent_hint(request_id, text)
        if intent_hint is not None:
            state.context["preclassified_intent"] = intent_hint.get("intent")
            state.context["tier_hint"] = {
                "source": intent_hint.get("kind", "execute_fast"),
                "hint_confidence": float(intent_hint.get("confidence", 1.0) or 1.0),
                "hint_intent_type": getattr(intent_hint.get("intent"), "type", ""),
                "hint_target": getattr(intent_hint.get("intent"), "query", getattr(intent_hint.get("intent"), "target", "")),
            }
        steps, metrics = await self._command_processor.build_steps(text, state)
        self._metrics["matched"] += metrics.get("matched", 0)
        self._metrics["fallback"] += metrics.get("fallback", 0)
        self._metrics["planner_calls"] += metrics.get("planner_calls", 0)
        self._metrics["planner_steps"] += metrics.get("planner_steps", 0)
        return steps

    def _dependencies_satisfied(self, step: ExecutionStep, completed_dependencies: set[str]) -> bool:
        if not step.dependencies:
            return True
        for dep in step.dependencies:
            if dep not in completed_dependencies:
                return False
        return True

    async def _execute_step(
        self,
        step: ExecutionStep,
        *,
        request_id: int | str | None = None,
    ) -> str:
        start = time.monotonic()
        last_result = "FAILED_EXECUTION"
        for attempt in range(1, step.policy.max_attempts + 1):
            if attempt > 1 and step.policy.backoff_seconds > 0:
                await asyncio.sleep(step.policy.backoff_seconds)
            try:
                if step.timeout_seconds is not None:
                    result = await asyncio.wait_for(
                        self._execute_structured_intent(step.intent, request_id=request_id),
                        timeout=step.timeout_seconds,
                    )
                else:
                    result = await self._execute_structured_intent(step.intent, request_id=request_id)
            except asyncio.TimeoutError:
                result = "FAILED_TIMEOUT"
            last_result = result
            if not str(result).startswith("FAILED"):
                step.metadata["attempts_used"] = attempt
                self._record_step_latency(step, time.monotonic() - start)
                return result
        step.metadata["attempts_used"] = step.policy.max_attempts
        fail_type = str(last_result).split(":")[0] if isinstance(last_result, str) else "FAILED_UNKNOWN"
        self._metrics["failure_types"][fail_type] = self._metrics["failure_types"].get(fail_type, 0) + 1
        self._record_step_latency(step, time.monotonic() - start)
        return last_result

    async def _execute_structured_intent(
        self,
        intent: ExecutionIntent | None,
        *,
        request_id: int | str | None = None,
    ) -> str:
        if intent is None:
            return "FAILED_UNSUPPORTED_INTENT"
        try:
            result = await self._application.execute_intent(intent, request_id)
        except Exception as exc:
            print(f"[ERROR] structured execution crash: {exc}", flush=True)
            return "FAILED_EXECUTION"
        return result or "SUCCESS"

    def _publish_terminal_event(self, request_id: int | str | None, result: str) -> None:
        if request_id is None:
            return
        if result and not str(result).startswith("FAILED"):
            self._application.publish_request_complete(request_id, result, None)
        else:
            self._application.publish_request_failed(request_id, result or "FAILED_EXECUTION")

    @staticmethod
    def _describe_intent(intent: ExecutionIntent) -> str:
        return describe_execution_intent(intent)

    # ── Fix 8: Per-step latency tracking ──────────────────────────────────

    def _record_step_latency(self, step: ExecutionStep, elapsed: float) -> None:
        intent_hash = self._hash_intent(step)
        latencies = self._metrics["step_latencies"]
        if intent_hash not in latencies:
            latencies[intent_hash] = []
        latencies[intent_hash].append(elapsed)
        if len(latencies[intent_hash]) > 50:
            latencies[intent_hash] = latencies[intent_hash][-50:]

    # ── Fix 3: Learning state persistence ─────────────────────────────────

    def _load_learning_state(self) -> None:
        """Load persisted learning state from JSON file (sync, called in __init__)."""
        if not self._learning_state_path:
            return
        path = self._learning_state_path
        if not path.exists():
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            self._confidence_penalties.update(data.get("confidence_penalties", {}))
            self._timeout_frequency.update(data.get("timeout_frequency", {}))
            self._strategy_scores.update(data.get("strategy_scores", {}))
            self._logger.info("Loaded learning state from %s", path)
        except Exception as exc:
            self._logger.warning("Failed to load learning state from %s: %s", path, exc)

    async def _persist_learning_state(self) -> None:
        """Persist learning state to JSON file with async lock."""
        if not self._learning_state_path:
            return
        async with self._learning_lock:
            data = {
                "confidence_penalties": dict(self._confidence_penalties),
                "timeout_frequency": dict(self._timeout_frequency),
                "strategy_scores": dict(self._strategy_scores),
            }
            path = self._learning_state_path
            try:
                await asyncio.to_thread(self._write_learning_state, path, data)
            except Exception as exc:
                self._logger.warning("Failed to persist learning state: %s", exc)

    @staticmethod
    def _write_learning_state(path: pathlib.Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)
