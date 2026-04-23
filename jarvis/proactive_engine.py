from __future__ import annotations


class _ProactiveEngine:
    def start(self) -> None:
        return

    def accept_suggestion(self, action_id: str) -> None:
        del action_id

    def reject_suggestion(self, action_id: str) -> None:
        del action_id


proactive = _ProactiveEngine()

