from __future__ import annotations

from src.agents.base import BaseAgent
from src.agents.forecast import exponential_forecast
from src.core.models import AgentAction, AgentLayer, AgentPrediction, TaskSpec
from src.metrics.provider import MetricSnapshot


class TransAgent(BaseAgent):
    def _state_from_snapshot(self, snapshot: MetricSnapshot) -> dict[str, float | int | str | bool]:
        return {
            "rtt_ms": snapshot.rtt_ms,
            "retransmission_rate": snapshot.retransmission_rate,
            "send_rate_mbps": snapshot.send_rate_mbps,
            "session_active": True,
            "congestion_signal": snapshot.utilization > 0.8,
        }

    def predict(self, task: TaskSpec) -> list[AgentPrediction]:
        rtt_history = self.history.get("rtt_ms", [task.qos.max_latency_ms * 0.6])
        loss_history = self.history.get("retransmission_rate", [task.qos.max_loss_rate * 0.7])
        return [
            AgentPrediction(
                agent_id=self.agent_id,
                layer=AgentLayer.TRANSPORT,
                metric="trans_latency_ms",
                horizon=self.horizon,
                values=exponential_forecast(rtt_history, self.horizon),
            ),
            AgentPrediction(
                agent_id=self.agent_id,
                layer=AgentLayer.TRANSPORT,
                metric="trans_loss_rate",
                horizon=self.horizon,
                values=exponential_forecast(loss_history, self.horizon),
            ),
        ]

    def decide(self, task: TaskSpec, predictions: list[AgentPrediction]) -> AgentAction:
        latency = max(item.values[-1] for item in predictions if item.metric == "trans_latency_ms")
        loss = max(item.values[-1] for item in predictions if item.metric == "trans_loss_rate")
        if latency > task.qos.max_latency_ms or loss > task.qos.max_loss_rate:
            return AgentAction(
                agent_id=self.agent_id,
                layer=AgentLayer.TRANSPORT,
                action_type="tune_transport_parameters",
                params={"send_rate_multiplier": 0.9, "retransmission_profile": "low-latency"},
                expected_effect={"latency_delta": 8.0, "loss_delta": 0.004},
            )
        return AgentAction(
            agent_id=self.agent_id,
            layer=AgentLayer.TRANSPORT,
            action_type="maintain_session",
        )

    def _execute_real_action(self, task: TaskSpec, action: AgentAction) -> None:
        if action.action_type != "tune_transport_parameters":
            return
        self.action_executor.tune_transport(
            send_rate_multiplier=float(action.params.get("send_rate_multiplier", 1.0)),
            tcp_nodelay=bool(action.params.get("tcp_nodelay", True)),
            tcp_congestion=str(action.params.get("tcp_congestion", "cubic")),
        )

    def feedback(self, task: TaskSpec, action: AgentAction) -> None:
        self.history.setdefault("trans_action_count", []).append(1.0 if action.expected_effect else 0.0)
        del self.history["trans_action_count"][:-self.history_window]
