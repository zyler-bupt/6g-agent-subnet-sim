from __future__ import annotations

from src.agents.base import BaseAgent
from src.agents.forecast import exponential_forecast
from src.core.models import AgentAction, AgentLayer, AgentPrediction, TaskSpec
from src.metrics.provider import MetricSnapshot


class NetAgent(BaseAgent):
    def _state_from_snapshot(self, snapshot: MetricSnapshot) -> dict[str, float | int | str | bool]:
        return {
            "throughput_mbps": snapshot.throughput_mbps,
            "available_bandwidth_mbps": snapshot.available_bandwidth_mbps,
            "latency_ms": snapshot.latency_ms,
            "jitter_ms": snapshot.jitter_ms,
            "loss_rate": snapshot.loss_rate,
            "utilization": snapshot.utilization,
            "queue_backlog": snapshot.queue_backlog,
        }

    def predict(self, task: TaskSpec) -> list[AgentPrediction]:
        bandwidth = self.history.get("available_bandwidth_mbps", [task.qos.min_bandwidth_mbps * 1.4])
        congestion = self.history.get("utilization", [0.55])
        return [
            AgentPrediction(
                agent_id=self.agent_id,
                layer=AgentLayer.NETWORK,
                metric="net_bandwidth_mbps",
                horizon=self.horizon,
                values=exponential_forecast(bandwidth, self.horizon),
            ),
            AgentPrediction(
                agent_id=self.agent_id,
                layer=AgentLayer.NETWORK,
                metric="net_utilization",
                horizon=self.horizon,
                values=exponential_forecast(congestion, self.horizon),
            ),
        ]

    def decide(self, task: TaskSpec, predictions: list[AgentPrediction]) -> AgentAction:
        bandwidth = min(item.values[-1] for item in predictions if item.metric == "net_bandwidth_mbps")
        utilization = max(item.values[-1] for item in predictions if item.metric == "net_utilization")
        if bandwidth < task.qos.min_bandwidth_mbps or utilization > 0.82:
            return AgentAction(
                agent_id=self.agent_id,
                layer=AgentLayer.NETWORK,
                action_type="raise_monitoring_and_bearer_advice",
                params={"sampling": "high", "bearer_advice": "prioritize_task_flow"},
                expected_effect={"bandwidth_delta": 4.0, "latency_delta": 4.0},
            )
        return AgentAction(
            agent_id=self.agent_id,
            layer=AgentLayer.NETWORK,
            action_type="keep_bearer",
        )

    def _execute_real_action(self, task: TaskSpec, action: AgentAction) -> None:
        if action.action_type != "raise_monitoring_and_bearer_advice":
            return
        self.action_executor.prioritize_task_flow(task.task_id, self.agent_id, dict(action.params))

    def feedback(self, task: TaskSpec, action: AgentAction) -> None:
        self.history.setdefault("net_action_count", []).append(1.0 if action.expected_effect else 0.0)
        del self.history["net_action_count"][:-self.history_window]
