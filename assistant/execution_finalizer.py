from __future__ import annotations

from dataclasses import dataclass

from assistant.contracts import TaskState


@dataclass(slots=True)
class ExecutionFinalization:
    response: str
    completed_steps: tuple[str, ...]
    skipped_steps: tuple[str, ...]
    failed_steps: tuple[str, ...]


class ExecutionFinalizer:
    """Builds stable end-of-run summaries for success, failure, and cancellation."""

    def finalize(self, task: TaskState, default_response: str) -> ExecutionFinalization:
        completed = tuple(step.description or step.action for step in task.steps if step.status == "completed")
        skipped = tuple(step.description or step.action for step in task.steps if step.status == "skipped")
        failed = tuple(step.description or step.action for step in task.steps if step.status == "failed")

        if task.status == "interrupted":
            response = self._interrupted_response(completed, skipped)
        elif task.status == "failed" and completed:
            response = self._partial_failure_response(default_response, completed, failed, skipped)
        else:
            response = default_response

        return ExecutionFinalization(
            response=response,
            completed_steps=completed,
            skipped_steps=skipped,
            failed_steps=failed,
        )

    @staticmethod
    def _interrupted_response(completed: tuple[str, ...], skipped: tuple[str, ...]) -> str:
        completed_part = (
            "Completed: " + "; ".join(_summarize(step) for step in completed[:3])
            if completed
            else "No steps completed."
        )
        skipped_part = (
            " Skipped: " + "; ".join(_summarize(step) for step in skipped[:3]) + "."
            if skipped
            else ""
        )
        return f"Cancelled safely. {completed_part}.{skipped_part}".replace("..", ".")

    @staticmethod
    def _partial_failure_response(
        default_response: str,
        completed: tuple[str, ...],
        failed: tuple[str, ...],
        skipped: tuple[str, ...],
    ) -> str:
        pieces = [default_response.strip()]
        if completed:
            pieces.append("Completed before the failure: " + "; ".join(_summarize(step) for step in completed[:3]) + ".")
        if failed:
            pieces.append("Failed at: " + "; ".join(_summarize(step) for step in failed[:2]) + ".")
        if skipped:
            pieces.append("Skipped: " + "; ".join(_summarize(step) for step in skipped[:3]) + ".")
        return " ".join(piece for piece in pieces if piece)


def _summarize(text: str) -> str:
    cleaned = " ".join(text.strip().split()).rstrip(".")
    return cleaned[:120]
