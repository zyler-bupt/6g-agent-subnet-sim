from __future__ import annotations

from src.agents.base import BaseAgent
from src.core.models import AgentAction, AgentLayer, AgentPrediction, TaskSpec
from src.metrics.provider import MetricSnapshot


class PhyAgentStub(BaseAgent):
    """P5 placeholder: keeps the interface without affecting P0-P4 behavior."""

    def _state_from_snapshot(self, snapshot: MetricSnapshot) -> dict[str, float | int | str | bool]:
        return {"phy_stub": True}

    def predict(self, task: TaskSpec) -> list[AgentPrediction]:
        return []

    def select_action(self, task: TaskSpec, predictions: list[AgentPrediction]) -> AgentAction:
        return AgentAction(
            agent_id=self.agent_id,
            layer=AgentLayer.PHYSICAL,
            action_type="phy_stub_noop",
        )

