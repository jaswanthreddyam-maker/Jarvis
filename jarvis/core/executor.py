from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from jarvis.core.cancellation import CancelledError, CancellationController
from jarvis.core.context import ExecutionPlan, ExecutionStep
from jarvis.core.safety import ExecutionGuard, ExecutionSandbox, ToolValidator
from jarvis.core.safety.permissions import PermissionLevel
from jarvis.core.tools import ToolContext, ToolRegistry, ToolResult


@dataclass(slots=True)
class StepExecutionResult:
    step_id: int
    action: str
    target: str
    params: dict[str, Any]
    success: bool
    message: str
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    status: str = "completed"
    permission_level: str = PermissionLevel.SAFE.value


@dataclass(slots=True)
class ExecutionReport:
    success: bool
    response: str
    steps: list[StepExecutionResult] = field(default_factory=list)

    def as_snapshot(self, *, user_input: str, plan: ExecutionPlan) -> dict[str, Any]:
        return {
            "status": "completed" if self.success else "failed",
            "user_input": user_input,
            "intent": plan.intent,
            "goal": plan.goal,
            "steps": [
                {
                    "step_id": item.step_id,
                    "action": item.action,
                    "target": item.target,
                    "params": dict(item.params),
                    "success": item.success,
                    "message": item.message,
                    "error": item.error,
                    "result": dict(item.data),
                    "status": item.status,
                    "permission_level": item.permission_level,
                }
                for item in self.steps
            ],
            "response": self.response,
        }


class ToolExecutor:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        validator: ToolValidator,
        guard: ExecutionGuard,
        sandbox: ExecutionSandbox,
        settings: Any,
        memory: Any,
        scheduler: Any | None = None,
        health_service: Any | None = None,
        event_bus: Any | None = None,
        cancellation_controller: CancellationController | None = None,
        default_mode: str = "sequential",
    ) -> None:
        self._registry = registry
        self._validator = validator
        self._guard = guard
        self._sandbox = sandbox
        self._settings = settings
        self._memory = memory
        self._scheduler = scheduler
        self._health_service = health_service
        self._event_bus = event_bus
        self._cancellation_controller = cancellation_controller or CancellationController()
        self._default_mode = default_mode

    async def execute_plan(
        self,
        plan: ExecutionPlan,
        *,
        request_id: str,
        goal: str,
        concurrency_mode: str | None = None,
        runtime_state: Any | None = None,
    ) -> ExecutionReport:
        mode = (concurrency_mode or self._default_mode).strip().lower()
        max_reflection_cycles = 2
        reflection_cycle = 0

        while reflection_cycle <= max_reflection_cycles:
            pending = {step.step_id: step for step in plan.steps}
            results: dict[int, StepExecutionResult] = {}
            ordered_results: list[StepExecutionResult] = []

            while pending:
                ready: list[ExecutionStep] = []
                for step_id in sorted(pending):
                    step = pending[step_id]
                    if not all(dep in results for dep in step.depends_on):
                        continue
                    failed_dependencies = [dep for dep in step.depends_on if not results[dep].success]
                    if failed_dependencies:
                        skipped = StepExecutionResult(
                            step_id=step.step_id,
                            action=step.action,
                            target=step.target,
                            params=dict(step.params),
                            success=False,
                            message="Skipped because a prerequisite step failed.",
                            error="dependency_failed",
                            data={"depends_on": list(step.depends_on)},
                            status="skipped",
                        )
                        results[step.step_id] = skipped
                        ordered_results.append(skipped)
                        pending.pop(step.step_id, None)
                        continue
                    ready.append(step)

                if not ready:
                    break

                if mode == "auto" and len(ready) > 1:
                    batch = await asyncio.gather(
                        *(self._execute_step(step, request_id=request_id, goal=goal, results=results, runtime_state=runtime_state) for step in ready)
                    )
                else:
                    batch = []
                    for step in ready:
                        batch.append(await self._execute_step(step, request_id=request_id, goal=goal, results=results, runtime_state=runtime_state))

                for result in batch:
                    results[result.step_id] = result
                    ordered_results.append(result)
                    pending.pop(result.step_id, None)

            success = all(item.success for item in ordered_results) if ordered_results else False
            if success or reflection_cycle >= max_reflection_cycles or runtime_state is None or not hasattr(runtime_state, "_planner"):
                response = self._compose_response(ordered_results, success=success, fallback=plan.fallback_response)
                return ExecutionReport(success=success, response=response, steps=ordered_results)

            reflection_cycle += 1
            failed_step = next((r for r in ordered_results if not r.success), None)
            if not failed_step:
                response = self._compose_response(ordered_results, success=success, fallback=plan.fallback_response)
                return ExecutionReport(success=success, response=response, steps=ordered_results)
                
            from jarvis.core.context import ExecutionPlan as ContextExecutionPlan
            plan_payload = {"intent": plan.intent, "goal": plan.goal, "steps": [
                {"tool": s.action, "target": s.target, "args": s.params, "description": s.description} for s in plan.steps
            ]}
            reflection = runtime_state._planner.reflect(
                goal=goal,
                current_plan=plan_payload,
                failed_step={"action": failed_step.action, "error": failed_step.error, "message": failed_step.message},
                execution_result={"status": "failed"}
            )
            if reflection.decision in ("abort", "ask_user"):
                response = reflection.message or reflection.question or "Task failed and I cannot safely recover."
                return ExecutionReport(success=False, response=response, steps=ordered_results)
                
            from jarvis.core.context import ExecutionStep as ContextExecutionStep
            plan.steps = [
                ContextExecutionStep(
                    step_id=i,
                    action=d.action,
                    target=d.target,
                    params=d.params,
                    depends_on=tuple(d.depends_on),
                    condition=d.condition,
                    param_bindings=d.param_bindings,
                    description=d.description,
                    confidence=d.confidence
                ) for i, d in enumerate(reflection.directives, start=1)
            ]

        # fallback return
        success = all(item.success for item in ordered_results) if ordered_results else False
        response = self._compose_response(ordered_results, success=success, fallback=plan.fallback_response)
        return ExecutionReport(success=success, response=response, steps=ordered_results)

    async def _execute_step(
        self,
        step: ExecutionStep,
        *,
        request_id: str,
        goal: str,
        results: dict[int, StepExecutionResult],
        runtime_state: Any | None,
    ) -> StepExecutionResult:
        token = self._cancellation_controller.token_for(request_id)
        if token is not None and token.is_cancelled():
            result = StepExecutionResult(
                step_id=step.step_id,
                action=step.action,
                target=step.target,
                params=dict(step.params),
                success=False,
                message="Request was cancelled before execution.",
                error="cancelled",
                status="cancelled",
            )
            self._publish_execution_update(request_id, step, result)
            return result

        resolved_params = self._bind_params(step, results)
        validation = self._validator.validate(
            registry=self._registry,
            action_name=step.action,
            params=resolved_params,
            target=step.target,
            project_root=self._settings.project_root,
        )
        if not validation.valid:
            result = StepExecutionResult(
                step_id=step.step_id,
                action=step.action,
                target=step.target,
                params=resolved_params,
                success=False,
                message=validation.reason,
                error="validation_blocked",
                status="failed",
                permission_level=validation.permission_level.value,
            )
            self._publish_execution_update(request_id, step, result)
            return result

        permission_level = validation.permission_level
        permission_block = self._permission_preflight(
            action_name=step.action,
            params=validation.sanitized_params,
            permission_level=permission_level,
        )
        if permission_block is not None:
            result = StepExecutionResult(
                step_id=step.step_id,
                action=step.action,
                target=step.target,
                params=validation.sanitized_params,
                success=False,
                message=permission_block.message,
                error=permission_block.error,
                data=dict(permission_block.data),
                status=permission_block.status,
                permission_level=permission_level.value,
            )
            self._publish_execution_update(request_id, step, result)
            return result

        guard_decision = self._guard.preflight(
            action_name=step.action,
            permission_level=permission_level,
            token=token,
        )
        if not guard_decision.allowed:
            result = StepExecutionResult(
                step_id=step.step_id,
                action=step.action,
                target=step.target,
                params=validation.sanitized_params,
                success=False,
                message=guard_decision.reason,
                error=guard_decision.error or "guard_blocked",
                data={"retry_after_seconds": guard_decision.retry_after_seconds},
                status="failed",
                permission_level=permission_level.value,
            )
            self._publish_execution_update(request_id, step, result)
            return result

        confirmation_block = self._confirmation_preflight(
            action_name=step.action,
            params=validation.sanitized_params,
            permission_level=permission_level,
        )
        if confirmation_block is not None:
            result = StepExecutionResult(
                step_id=step.step_id,
                action=step.action,
                target=step.target,
                params=validation.sanitized_params,
                success=False,
                message=confirmation_block.message,
                error=confirmation_block.error,
                data=dict(confirmation_block.data),
                status=confirmation_block.status,
                permission_level=permission_level.value,
            )
            self._publish_execution_update(request_id, step, result)
            return result

        sandbox = self._sandbox.evaluate(
            action_name=step.action,
            params=validation.sanitized_params,
            project_root=self._settings.project_root,
        )
        if not sandbox.allowed:
            result = StepExecutionResult(
                step_id=step.step_id,
                action=step.action,
                target=step.target,
                params=validation.sanitized_params,
                success=False,
                message=sandbox.reason,
                error="sandbox_blocked",
                status="failed",
                permission_level=permission_level.value,
            )
            self._publish_execution_update(request_id, step, result)
            return result

        context = ToolContext(
            request_id=request_id,
            step_id=step.step_id,
            goal=goal,
            settings=self._settings,
            project_root=self._settings.project_root,
            memory=self._memory,
            scheduler=self._scheduler,
            health_service=self._health_service,
            safety_guard=self._guard,
            cancellation_token=token,
            event_bus=self._event_bus,
            runtime_state=runtime_state,
        )
        self._publish_execution_update(
            request_id,
            step,
            {
                "status": "started",
                "success": True,
                "message": step.description or f"Running {step.action}.",
                "permission_level": permission_level.value,
            },
        )
        if self._event_bus is not None:
            self._event_bus.publish(
                "action.started",
                {
                    "request_id": request_id,
                    "step_id": step.step_id,
                    "action": step.action,
                    "target": step.target,
                    "permission_level": permission_level.value,
                },
            )

        try:
            tool_result = await asyncio.wait_for(
                self._registry.call_async(step.action, sandbox.sanitized_params, context),
                timeout=guard_decision.timeout_seconds,
            )
        except asyncio.TimeoutError:
            tool_result = ToolResult(
                success=False,
                message=f"The step '{step.action}' timed out and was cancelled.",
                error="action_timeout",
                data={"execution_state": "cancelled"},
            )
        except CancelledError as exc:
            tool_result = ToolResult(
                success=False,
                message=str(exc) or "The request was cancelled.",
                error="cancelled",
                data={"execution_state": "cancelled"},
            )

        result = StepExecutionResult(
            step_id=step.step_id,
            action=step.action,
            target=step.target,
            params=dict(sandbox.sanitized_params),
            success=tool_result.success,
            message=tool_result.message,
            error=tool_result.error,
            data=dict(tool_result.data),
            status=tool_result.status,
            permission_level=permission_level.value,
        )
        if self._event_bus is not None:
            self._event_bus.publish(
                "action.completed" if result.success else "action.failed",
                {
                    "request_id": request_id,
                    "step_id": step.step_id,
                    "action": step.action,
                    "success": result.success,
                    "message": result.message,
                    "error": result.error,
                    "permission_level": permission_level.value,
                },
            )
        self._publish_execution_update(request_id, step, result)
        return result

    def _permission_preflight(
        self,
        *,
        action_name: str,
        params: dict[str, Any],
        permission_level: PermissionLevel,
    ) -> ToolResult | None:
        normalized_action = str(action_name).strip().lower()
        if self._settings.safe_mode and permission_level != PermissionLevel.SAFE:
            return ToolResult(
                success=False,
                message=f"Safe mode blocked '{normalized_action}' because it is not a SAFE action.",
                error="safe_mode_blocked",
            )
        return None

    @staticmethod
    def _confirmation_preflight(
        *,
        action_name: str,
        params: dict[str, Any],
        permission_level: PermissionLevel,
    ) -> ToolResult | None:
        normalized_action = str(action_name).strip().lower()
        if permission_level != PermissionLevel.DANGEROUS or bool(params.get("_confirmed", False)):
            return None
        return ToolResult(
            success=False,
            message=f"Confirmation required before running dangerous tool '{normalized_action}'.",
            error="confirmation_required",
            data={"needs_confirmation": True},
        )

    @staticmethod
    def _bind_params(step: ExecutionStep, results: dict[int, StepExecutionResult]) -> dict[str, Any]:
        bound = dict(step.params)
        for key, source in dict(step.param_bindings).items():
            if key in bound:
                continue
            bound[key] = ToolExecutor._resolve_binding(source, results)
        return bound

    @staticmethod
    def _resolve_binding(source: str, results: dict[int, StepExecutionResult]) -> Any:
        normalized = str(source).strip()
        if not normalized.startswith("step_"):
            return None
        head, _, tail = normalized.partition(".")
        try:
            step_id = int(head.removeprefix("step_"))
        except ValueError:
            return None
        result = results.get(step_id)
        if result is None:
            return None
        current: Any = {"data": dict(result.data), "message": result.message, "target": result.target}
        for segment in filter(None, tail.split(".")):
            if isinstance(current, dict):
                current = current.get(segment)
            else:
                current = getattr(current, segment, None)
        return current

    @staticmethod
    def _compose_response(results: list[StepExecutionResult], *, success: bool, fallback: str) -> str:
        if not results:
            return fallback or "I could not build a reliable execution plan."
        if success:
            messages = [item.message for item in results if item.message]
            return " ".join(messages).strip() or "Completed successfully."
        for item in results:
            if not item.success and item.message:
                return item.message
        return fallback or "The plan failed before it could complete."

    def _publish_execution_update(self, request_id: str, step: ExecutionStep, payload: StepExecutionResult | dict[str, Any]) -> None:
        if self._event_bus is None:
            return

        if isinstance(payload, StepExecutionResult):
            message = payload.message
            success = payload.success
            status = payload.status
            error = payload.error
            permission_level = payload.permission_level
        else:
            message = str(payload.get("message", "") or "").strip()
            success = bool(payload.get("success", True))
            status = str(payload.get("status", "started") or "started").strip()
            error = str(payload.get("error", "") or "").strip() or None
            permission_level = str(payload.get("permission_level", "") or "").strip()

        self._event_bus.publish(
            "runtime.execution_update",
            {
                "type": "execution_update",
                "request_id": request_id,
                "step_id": step.step_id,
                "tool": step.action,
                "target": step.target,
                "status": status,
                "success": success,
                "message": message,
                "error": error,
                "permission_level": permission_level,
            },
        )
