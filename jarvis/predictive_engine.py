from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Prediction:
    action_id: str
    label: str
    reason: str
    confidence: float


class _Predictor:
    def __init__(self) -> None:
        self._predictions: list[Prediction] = []

    def start(self) -> None:
        return

    def get_predictions(self) -> list[Prediction]:
        return list(self._predictions)


predictor = _Predictor()

