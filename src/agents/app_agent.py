from __future__ import annotations

from src.agents.base import BaseAgent, exponential_forecast
from src.core.models import AgentAction, AgentLayer, AgentPrediction, TaskSpec
from src.metrics.provider import MetricSnapshot


class AppAgent(BaseAgent):
    def _state_from_snapshot(self, snapshot: MetricSnapshot) -> dict[str, float | int | str | bool]:
        return {
            "data_rate_mbps": snapshot.app_rate_mbps,
            "task_stage": "streaming",
            "priority": 1,
            "quality_score": 0.86,
        }

    def predict(self, task: TaskSpec) -> list[AgentPrediction]:
        history = self.history.get("data_rate_mbps", [task.qos.min_bandwidth_mbps * 0.75])
        return [
            AgentPrediction(
                agent_id=self.agent_id,
                layer=AgentLayer.APPLICATION,
                metric="app_demand_mbps",
                horizon=self.horizon,
                values=exponential_forecast(history, self.horizon),
            )
        ]

    def select_action(self, task: TaskSpec, predictions: list[AgentPrediction]) -> AgentAction:
        peak = max(predictions[0].values)
        if peak > task.qos.min_bandwidth_mbps:
            return AgentAction(
                agent_id=self.agent_id,
                layer=AgentLayer.APPLICATION,
                action_type="reduce_noncritical_quality",
                params={"bitrate_multiplier": 0.82, "resolution": "adaptive"},
                expected_effect={"app_rate_multiplier": 0.82},
            )
        return AgentAction(
            agent_id=self.agent_id,
            layer=AgentLayer.APPLICATION,
            action_type="keep_application_profile",
        )

