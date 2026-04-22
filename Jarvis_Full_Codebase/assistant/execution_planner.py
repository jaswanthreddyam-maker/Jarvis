from __future__ import annotations

from dataclasses import dataclass

from assistant.command_router import CommandParse, CommandRouter
from assistant.context_continuity import ContextContinuityEngine
from assistant.contracts import RiskLevel, StepDefinition, TaskPlan
from assistant.session_memory import SessionMemory
from assistant.scope_validator import ActionScopeValidator


@dataclass(slots=True)
class PlannedCommand:
    parse: CommandParse
    plan: TaskPlan


class ExecutionPlanner:
    """Turns parsed local intents into executable ordered task plans."""

    def __init__(
        self,
        router: CommandRouter | None = None,
        session_memory: SessionMemory | None = None,
        continuity: ContextContinuityEngine | None = None,
        scope_validator: ActionScopeValidator | None = None,
    ) -> None:
        self._router = router or CommandRouter()
        self._session_memory = session_memory or SessionMemory()
        self._continuity = continuity or ContextContinuityEngine()
        self._scope_validator = scope_validator or ActionScopeValidator()

    @property
    def session_memory(self) -> SessionMemory:
        return self._session_memory

    def parse(self, text: str) -> CommandParse:
        memory = self._session_memory.snapshot()
        parsed = self._router.route_many(text, memory=memory)
        if parsed.intents:
            parsed.intents, clarification_question = self._continuity.enrich(
                parsed.intents,
                memory=memory,
                last_app=memory.last_app,
                _last_action=memory.last_command,
                _last_target=memory.last_target,
            )
            if clarification_question:
                parsed.clarification_question = clarification_question
            parsed.confidence = self._average_confidence(parsed.intents)
        return parsed

    def build_plan(self, goal: str) -> TaskPlan:
        parsed = self.parse(goal)
        if parsed.clarification_question:
            return TaskPlan(
                intent="planner_message",
                goal=goal,
                fallback_response=parsed.clarification_question,
                clarification_question=parsed.clarification_question,
            )

        if not parsed.intents:
            return TaskPlan(
                intent="planner_message",
                goal=goal,
                fallback_response="I could not turn that into a reliable local action yet.",
            )

        steps = []
        for index, intent in enumerate(parsed.intents, start=1):
            previous_step = steps[-1] if steps else None
            step = self._step_from_intent(intent, step_id=index, previous_step=previous_step)
            steps.append(self._scope_validator.annotate_step(step))
        for step in steps:
            if (
                step.action == "open_app"
                and step.target.strip().lower() in {"chrome", "google chrome", "comet", "edge", "microsoft edge", "firefox"}
                and any(step.step_id in candidate.depends_on for candidate in steps)
            ):
                step.fallback_action = ""
                step.fallback_target = ""
                step.fallback_params = {}
                step.fallback_description = ""
                step.fallback_verification = ""
        overall_risk = max((step.risk_level for step in steps), key=self._risk_order, default=RiskLevel.SAFE)
        intent_name = steps[0].action if len(steps) == 1 else "multi_step_command"
        return TaskPlan(
            intent=intent_name,
            goal=goal,
            steps=steps,
            overall_risk=overall_risk,
        )

    def remember_success(self, user_input: str, plan: TaskPlan) -> None:
        self._session_memory.remember_plan(user_input, plan)

    def remember_action(
        self,
        *,
        user_input: str,
        action: str,
        target: str = "",
        params: dict[str, object] | None = None,
    ) -> None:
        self._session_memory.remember_action(
            user_input=user_input,
            action=action,
            target=target,
            params=dict(params or {}),
        )

    @staticmethod
    def _step_from_intent(intent, *, step_id: int, previous_step: StepDefinition | None) -> StepDefinition:
        action = intent.action
        verification = ExecutionPlanner._verification_for(action, intent.target)
        fallback = ExecutionPlanner._fallback_for(action, intent.target, intent.params)
        risk_level = ExecutionPlanner._risk_for(action)
        depends_on = tuple([previous_step.step_id]) if intent.depends_on_previous and previous_step is not None else tuple()
        return StepDefinition(
            action=action,
            step_id=step_id,
            target=intent.target,
            params=dict(intent.params),
            depends_on=depends_on,
            param_bindings=dict(intent.param_bindings),
            description=intent.description,
            verification=verification,
            fallback_action=fallback["action"],
            fallback_target=fallback["target"],
            fallback_params=fallback["params"],
            fallback_description=fallback["description"],
            fallback_verification=fallback["verification"],
            risk_level=risk_level,
            max_retries=1 if action in {"open_app", "open_url", "install_app"} else 0,
        )

    @staticmethod
    def _verification_for(action: str, target: str) -> str:
        return {
            "remember_fact": "confirm the fact is stored in memory",
            "recall_memory": "confirm memory lookup completed",
            "set_reminder": "confirm the reminder is scheduled",
            "open_url": f"confirm the URL for {target} was prepared",
            "search_web": "confirm the search URL was prepared",
            "search_youtube": "confirm the YouTube search URL was prepared",
            "play_youtube": "confirm YouTube video playback started",
            "open_app": f"confirm {target} launched",
            "focus_app": "confirm the application was focused",
            "close_app": "confirm the application close request was sent",
            "create_file": "confirm the file was created",
            "overwrite_file": "confirm the file was overwritten",
            "read_file": "confirm the file contents were returned",
            "delete_file": "confirm the file was deleted",
            "set_volume": "confirm the volume command was sent",
            "open_explorer": "confirm explorer opened",
            "get_clipboard": "confirm clipboard text was returned",
            "set_clipboard": "confirm clipboard text was updated",
            "minimize_window": "confirm the window was minimized",
            "switch_window": "confirm the window switched",
            "close_active_window": "confirm the close request was sent",
            "get_time": "confirm the local time was returned",
            "health_check": "confirm the runtime health payload was returned",
            "report_capabilities": "confirm the capabilities report was returned",
        }.get(action, "action_result")

    @staticmethod
    def _fallback_for(action: str, target: str, params: dict[str, object]) -> dict[str, object]:
        if action == "open_app":
            return {
                "action": "search_web",
                "target": target,
                "params": {"query": target},
                "description": f"Search the web for {target}.",
                "verification": "confirm the search URL was prepared",
            }
        if action == "install_app":
            query = f"install {target} on windows"
            return {
                "action": "search_web",
                "target": target,
                "params": {"query": query},
                "description": f"Search installation instructions for {target}.",
                "verification": "confirm the search URL was prepared",
            }
        if action == "set_reminder":
            message = str(params.get("message", target)).strip()
            delay_seconds = int(params.get("delay_seconds", 0))
            return {
                "action": "remember_fact",
                "target": message,
                "params": {
                    "content": f"Reminder request: {message} in {delay_seconds} seconds",
                    "namespace": "system",
                    "category": "reminder_fallback",
                },
                "description": "Store the reminder request as a note.",
                "verification": "confirm the fact is stored in memory",
            }
        if action == "play_youtube":
            query = str(params.get("query") or target).strip()
            return {
                "action": "search_youtube",
                "target": query,
                "params": {"query": query},
                "description": f"Search YouTube for {query}.",
                "verification": "confirm the YouTube search URL was prepared",
            }
        return {"action": "", "target": "", "params": {}, "description": "", "verification": ""}

    @staticmethod
    def _risk_for(action: str) -> RiskLevel:
        if action in {"delete_file", "overwrite_file", "install_app"}:
            return RiskLevel.HIGH
        if action in {"close_app"}:
            return RiskLevel.MEDIUM
        return RiskLevel.SAFE

    @staticmethod
    def _risk_order(risk: RiskLevel) -> int:
        return {
            RiskLevel.SAFE: 0,
            RiskLevel.LOW: 1,
            RiskLevel.MEDIUM: 2,
            RiskLevel.HIGH: 3,
            RiskLevel.CRITICAL: 4,
        }[risk]

    @staticmethod
    def _average_confidence(intents) -> float:
        if not intents:
            return 0.0
        return sum(intent.confidence for intent in intents) / len(intents)
