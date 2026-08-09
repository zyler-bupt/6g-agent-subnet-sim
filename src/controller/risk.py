from __future__ import annotations

from dataclasses import dataclass

from src.core.models import AgentLayer, AgentPrediction, TaskSpec


@dataclass(frozen=True)
class RiskWeights:
    lambda_r: float = 0.05
    lambda_d: float = 0.02
    lambda_p: float = 10.0
    lambda_h: float = 1.0


class RiskCalculator:
    def __init__(self, weights: RiskWeights | None = None) -> None:
        self.weights = weights or RiskWeights()

    def risk(self, task: TaskSpec, predictions: list[AgentPrediction]) -> float:
        app = self._max_metric(predictions, "app_demand_mbps")
        net = self._min_metric(predictions, "net_bandwidth_mbps")
        latency = self._max_metric(predictions, "trans_latency_ms")
        loss = self._max_metric(predictions, "trans_loss_rate")
        phy = self._physical_risk(predictions, task.qos.min_reliability)
        return (
            self.weights.lambda_r * max(app - net, 0.0)
            + self.weights.lambda_d * max(latency - task.qos.max_latency_ms, 0.0)
            + self.weights.lambda_p * max(loss - task.qos.max_loss_rate, 0.0)
            + self.weights.lambda_h * phy
        )

    @staticmethod
    def _max_metric(predictions: list[AgentPrediction], metric: str) -> float:
        values = [value for item in predictions if item.metric == metric for value in item.values]
        return max(values) if values else 0.0

    @staticmethod
    def _min_metric(predictions: list[AgentPrediction], metric: str) -> float:
        values = [value for item in predictions if item.metric == metric for value in item.values]
        return min(values) if values else 0.0

    @staticmethod
    def _physical_risk(
        predictions: list[AgentPrediction],
        required_reliability: float,
    ) -> float:
        values = [
            max(required_reliability - value, 0.0)
            for item in predictions
            if item.layer == AgentLayer.PHYSICAL
            and item.metric == "phy_reliability"
            for value in item.values
        ]
        return max(values) if values else 0.0
