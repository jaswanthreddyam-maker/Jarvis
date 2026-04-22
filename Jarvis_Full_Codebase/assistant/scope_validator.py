from __future__ import annotations

from dataclasses import dataclass

from assistant.contracts import RiskLevel, StepDefinition, StepState


_DESTRUCTIVE_ACTIONS = frozenset({"delete_file", "overwrite_file", "install_app", "system_action"})
_SYSTEM_POWER_ACTIONS = frozenset({"shutdown", "restart", "reboot"})
_BULK_MARKERS = ("all", "everything", "entire", "folder", "directory", "*.","*","?")


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


@dataclass(slots=True)
class ScopeAssessment:
    action: str
    target: str
    scope: str = "single"
    risk_level: RiskLevel = RiskLevel.SAFE
    confirmation_level: int = 0
    safety_level: str = "SAFE"
    valid: bool = True
    reason: str = "Validated."
    estimated_items: int = 1

    @property
    def requires_confirmation(self) -> bool:
        return self.confirmation_level > 0


class ActionScopeValidator:
    """Analyzes destructive action scope before execution and confirmation."""

    def assess(self, action: str, target: str, params: dict[str, object] | None = None) -> ScopeAssessment:
        params = dict(params or {})
        normalized_action = _normalize(action)
        normalized_target = _normalize(
            target
            or str(params.get("name") or params.get("app_name") or params.get("action_type") or "")
        )

        if normalized_action not in _DESTRUCTIVE_ACTIONS:
            return ScopeAssessment(
                action=normalized_action,
                target=normalized_target,
                risk_level=RiskLevel.SAFE,
                confirmation_level=0,
                safety_level="SAFE",
            )

        if normalized_action == "system_action":
            if normalized_target not in _SYSTEM_POWER_ACTIONS:
                return ScopeAssessment(
                    action=normalized_action,
                    target=normalized_target,
                    valid=False,
                    risk_level=RiskLevel.CRITICAL,
                    confirmation_level=2,
                    safety_level="BLOCKED",
                    reason="System actions must name an allowed power action explicitly.",
                    estimated_items=1,
                )
            return ScopeAssessment(
                action=normalized_action,
                target=normalized_target,
                scope="system",
                risk_level=RiskLevel.CRITICAL,
                confirmation_level=2,
                safety_level="CONFIRMATION REQUIRED",
                reason="This action can interrupt or stop the computer.",
                estimated_items=1,
            )

        if not normalized_target:
            return ScopeAssessment(
                action=normalized_action,
                target=normalized_target,
                valid=False,
                risk_level=RiskLevel.HIGH,
                confirmation_level=1,
                safety_level="BLOCKED",
                reason="A destructive action needs an explicit target before it can run.",
                estimated_items=0,
            )

        if self._looks_bulk(normalized_target):
            return ScopeAssessment(
                action=normalized_action,
                target=normalized_target,
                scope="bulk",
                risk_level=RiskLevel.CRITICAL,
                confirmation_level=2,
                safety_level="CONFIRMATION REQUIRED",
                reason="This action appears to affect multiple files or a broad scope.",
                estimated_items=self._estimate_items(normalized_target),
            )

        return ScopeAssessment(
            action=normalized_action,
            target=normalized_target,
            scope="single",
            risk_level=RiskLevel.HIGH,
            confirmation_level=1,
            safety_level="CONFIRMATION REQUIRED",
            reason="This action changes or removes data.",
            estimated_items=1,
        )

    def annotate_step(self, step: StepDefinition) -> StepDefinition:
        assessment = self.assess(step.action, step.target, step.params)
        step.params.setdefault("_scope", assessment.scope)
        step.params.setdefault("_safety_level", assessment.safety_level)
        step.params.setdefault("_confirmation_level", assessment.confirmation_level)
        step.params.setdefault("_validation_reason", assessment.reason)
        step.params.setdefault("_estimated_items", assessment.estimated_items)
        if assessment.risk_level.value not in {RiskLevel.SAFE.value, ""}:
            step.risk_level = max(step.risk_level, assessment.risk_level, key=self._risk_order)
        return step

    def annotate_state(self, step: StepState) -> StepState:
        assessment = self.assess(step.action, step.target, step.params)
        step.params.setdefault("_scope", assessment.scope)
        step.params.setdefault("_safety_level", assessment.safety_level)
        step.params.setdefault("_confirmation_level", assessment.confirmation_level)
        step.params.setdefault("_validation_reason", assessment.reason)
        step.params.setdefault("_estimated_items", assessment.estimated_items)
        if assessment.risk_level.value not in {"", RiskLevel.SAFE.value}:
            step.risk_level = max(step.risk_level, assessment.risk_level.value, key=self._risk_rank)
        return step

    @staticmethod
    def _looks_bulk(target: str) -> bool:
        if any(marker in target for marker in _BULK_MARKERS):
            return True
        return any(separator in target for separator in (",", ";", " and "))

    @staticmethod
    def _estimate_items(target: str) -> int:
        if any(marker in target for marker in _BULK_MARKERS):
            return 10
        return max(2, target.count(",") + target.count(";") + target.count(" and ") + 1)

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
    def _risk_rank(value: str) -> int:
        return {
            RiskLevel.SAFE.value: 0,
            RiskLevel.LOW.value: 1,
            RiskLevel.MEDIUM.value: 2,
            RiskLevel.HIGH.value: 3,
            RiskLevel.CRITICAL.value: 4,
        }.get(value, 0)
