from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.core.models import AgentAction, AgentCard, AgentPrediction, AgentState, TaskSpec
from src.metrics.provider import MetricProvider, MetricSnapshot
from src.agents.forecast import exponential_forecast, forecast_history


@dataclass
class BaseAgent:
    card: AgentCard
    metric_provider: MetricProvider
    horizon: int = 3
    history_window: int = 10
    action_executor: Any | None = None
    forecast_method: str = "adaptive"
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
        return self.decide(task, predictions)

    def decide(self, task: TaskSpec, predictions: list[AgentPrediction]) -> AgentAction:
        raise NotImplementedError

    def apply(self, task: TaskSpec, action: AgentAction) -> AgentAction:
        return self.execute(task, action)

    def execute(self, task: TaskSpec, action: AgentAction) -> AgentAction:
        if self.action_executor is not None:
            self._execute_real_action(task, action)
        else:
            self.metric_provider.apply_effect(task.task_id, action.expected_effect)
        self.feedback(task, action)
        return action

    def feedback(self, task: TaskSpec, action: AgentAction) -> None:
        pass

    def run_step(self, task: TaskSpec, timestamp: float) -> tuple[list[AgentPrediction], AgentAction]:
        self.sense(task, timestamp)
        predictions = self.predict(task)
        action = self.decide(task, predictions)
        return predictions, self.execute(task, action)

    def _state_from_snapshot(self, snapshot: MetricSnapshot) -> dict[str, float | int | str | bool]:
        raise NotImplementedError

    def _execute_real_action(self, task: TaskSpec, action: AgentAction) -> None:
        # Subclasses own their action functions. This fallback keeps no-op
        # actions harmless in real mode.
        return None

    def _record(self, values: dict[str, float | int | str | bool]) -> None:
        for key, value in values.items():
            if isinstance(value, (int, float)):
                items = self.history.setdefault(key, [])
                items.append(float(value))
                del items[:-self.history_window]
