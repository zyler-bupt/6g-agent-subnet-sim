from __future__ import annotations

from dataclasses import dataclass, field

from src.core.models import AgentAction, AgentCard, AgentPrediction, AgentState, TaskSpec
from src.metrics.provider import MetricProvider, MetricSnapshot


def exponential_forecast(history: list[float], horizon: int, alpha: float = 0.55) -> tuple[float, ...]:
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if not history:
        raise ValueError("history must not be empty")
    estimate = history[0]
    for value in history[1:]:
        estimate = alpha * value + (1.0 - alpha) * estimate
    if len(history) > 1:
        trend = (history[-1] - history[max(0, len(history) - 4)]) / min(3, len(history) - 1)
    else:
        trend = 0.0
    return tuple(max(0.0, estimate + trend * (idx + 1)) for idx in range(horizon))


@dataclass
class BaseAgent:
    card: AgentCard
    metric_provider: MetricProvider
    horizon: int = 3
    history: dict[str, list[float]] = field(default_factory=dict)

    @property
    def agent_id(self) -> str:
        return self.card.agent_id

    def sense(self, task: TaskSpec, timestamp: float) -> AgentState:
        snapshot = self.metric_provider.snapshot(task.task_id, self.agent_id, timestamp)
        values = self._state_from_snapshot(snapshot)
        self._record(values)
        return AgentState(values=values)

    def predict(self, task: TaskSpec) -> list[AgentPrediction]:
        raise NotImplementedError

    def select_action(self, task: TaskSpec, predictions: list[AgentPrediction]) -> AgentAction:
        raise NotImplementedError

    def apply(self, task: TaskSpec, action: AgentAction) -> AgentAction:
        self.metric_provider.apply_effect(task.task_id, action.expected_effect)
        return action

    def run_step(self, task: TaskSpec, timestamp: float) -> tuple[list[AgentPrediction], AgentAction]:
        self.sense(task, timestamp)
        predictions = self.predict(task)
        action = self.select_action(task, predictions)
        return predictions, self.apply(task, action)

    def _state_from_snapshot(self, snapshot: MetricSnapshot) -> dict[str, float | int | str | bool]:
        raise NotImplementedError

    def _record(self, values: dict[str, float | int | str | bool]) -> None:
        for key, value in values.items():
            if isinstance(value, (int, float)):
                items = self.history.setdefault(key, [])
                items.append(float(value))
                del items[:-12]

