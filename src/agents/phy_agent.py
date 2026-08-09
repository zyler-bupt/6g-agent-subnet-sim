from __future__ import annotations

from dataclasses import replace

from src.agents.base import BaseAgent
from src.agents.forecast import forecast_history
from src.core.models import (
    AgentAction,
    AgentLayer,
    AgentPrediction,
    AgentState,
    PhysicalAgentState,
    PhysicalResourceBinding,
    TaskSpec,
)
from src.metrics.provider import MetricSnapshot


class PhyAgent(BaseAgent):
    """Minimal physical-layer resource observer and reservation endpoint.

    Candidate actions are advisory. Resource state changes only through the
    explicit allocate_resource/release_resource methods invoked by a verified
    Controller transaction.
    """

    @property
    def resource_bindings(self) -> dict[str, PhysicalResourceBinding]:
        bindings = getattr(self, "_resource_bindings", None)
        if bindings is None:
            bindings = {}
            self._resource_bindings = bindings
        return bindings

    def sense(self, task: TaskSpec, timestamp: float) -> AgentState:
        """Report Controller-owned access state without perturbing flow probes.

        The minimal stage-2 physical model is configuration/state based.  It
        therefore does not consume the transport/network metric provider's
        pseudo-random sample stream; adding a pAgent must not change the samples
        observed by the other three layers under the same seed.
        """

        state = self.report_state()
        values: dict[str, float | int | str | bool] = {
            "signal_quality": state.signal_quality,
            "available_capacity_mbps": state.available_capacity_mbps,
            "resource_utilization": state.resource_utilization,
            "reliability": state.reliability,
            "online": state.online,
        }
        self._record(values)
        return AgentState(values)

    @property
    def total_capacity_mbps(self) -> float:
        return float(self.card.state.values.get("total_capacity_mbps", 100.0))

    def configure_capacity(self, capacity_mbps: float) -> None:
        if capacity_mbps <= 0.0:
            raise ValueError("physical capacity must be positive")
        values = dict(self.card.state.values)
        values["total_capacity_mbps"] = float(capacity_mbps)
        self.card = replace(self.card, state=AgentState(values))

    def _state_from_snapshot(
        self,
        snapshot: MetricSnapshot,
    ) -> dict[str, float | int | str | bool]:
        total = self.total_capacity_mbps
        reserved = sum(
            binding.reserved_capacity_mbps
            for binding in self.resource_bindings.values()
            if binding.active
        )
        available = min(
            max(0.0, total - reserved),
            max(0.0, snapshot.available_bandwidth_mbps),
        )
        signal_quality = min(1.0, max(0.0, snapshot.phy_stability))
        reliability = min(signal_quality, max(0.0, 1.0 - snapshot.loss_rate))
        utilization = max(snapshot.utilization, reserved / max(total, 1e-9))
        return {
            "signal_quality": signal_quality,
            "available_capacity_mbps": available,
            "resource_utilization": min(1.0, utilization),
            "reliability": reliability,
            "online": self.card.online,
        }

    def predict(self, task: TaskSpec) -> list[AgentPrediction]:
        state = self.report_state()
        reliability_history = self.history.get("reliability", [state.reliability])
        capacity_history = self.history.get(
            "available_capacity_mbps",
            [state.available_capacity_mbps],
        )
        return [
            AgentPrediction(
                agent_id=self.agent_id,
                layer=AgentLayer.PHYSICAL,
                metric="phy_reliability",
                horizon=self.horizon,
                values=forecast_history(
                    reliability_history,
                    self.horizon,
                    method=self.forecast_method,
                ),
            ),
            AgentPrediction(
                agent_id=self.agent_id,
                layer=AgentLayer.PHYSICAL,
                metric="phy_available_capacity_mbps",
                horizon=self.horizon,
                values=forecast_history(
                    capacity_history,
                    self.horizon,
                    method=self.forecast_method,
                ),
            ),
        ]

    def decide(self, task: TaskSpec, predictions: list[AgentPrediction]) -> AgentAction:
        reliability_values = [
            value
            for prediction in predictions
            if prediction.metric == "phy_reliability"
            for value in prediction.values
        ]
        reliability = min(reliability_values, default=self.report_state().reliability)
        if reliability < task.qos.min_reliability:
            return AgentAction(
                agent_id=self.agent_id,
                layer=AgentLayer.PHYSICAL,
                action_type="SWITCH_ACCESS",
                params={"reason": "predicted_reliability_below_requirement"},
            )
        return AgentAction(
            agent_id=self.agent_id,
            layer=AgentLayer.PHYSICAL,
            action_type="KEEP_RESOURCE",
        )

    def execute(self, task: TaskSpec, action: AgentAction) -> AgentAction:
        # A proposal is not an authorization. The Controller owns all mutations.
        self.feedback(task, action)
        return action

    def propose_for_edge(
        self,
        required_mbps: float,
        *,
        currently_bound: bool,
        release: bool = False,
    ) -> AgentAction:
        if release:
            action_type = "RELEASE_RESOURCE" if currently_bound else "KEEP_RESOURCE"
        elif currently_bound:
            action_type = "KEEP_RESOURCE"
        elif self.can_allocate(required_mbps):
            action_type = "ALLOCATE_RESOURCE"
        else:
            action_type = "SWITCH_ACCESS"
        return AgentAction(
            agent_id=self.agent_id,
            layer=AgentLayer.PHYSICAL,
            action_type=action_type,
            params={"required_mbps": required_mbps},
        )

    def can_allocate(self, required_mbps: float, binding_id: str = "") -> bool:
        if required_mbps < 0.0:
            return False
        existing = self.resource_bindings.get(binding_id)
        reclaim = existing.reserved_capacity_mbps if existing and existing.active else 0.0
        return self.card.online and required_mbps <= self.available_capacity_mbps + reclaim + 1e-9

    @property
    def available_capacity_mbps(self) -> float:
        reserved = sum(
            binding.reserved_capacity_mbps
            for binding in self.resource_bindings.values()
            if binding.active
        )
        return max(0.0, self.total_capacity_mbps - reserved)

    def allocate_resource(
        self,
        task_id: str,
        edge_id: str,
        required_mbps: float,
        version: int,
    ) -> PhysicalResourceBinding:
        binding_id = physical_binding_id(task_id, edge_id, self.agent_id)
        if required_mbps < 0.0:
            raise ValueError("required physical capacity must not be negative")
        if not self.can_allocate(required_mbps, binding_id):
            raise ValueError(
                f"physical capacity exceeded at {self.agent_id}: "
                f"required={required_mbps:g}, available={self.available_capacity_mbps:g}"
            )
        binding = PhysicalResourceBinding(
            binding_id=binding_id,
            task_id=task_id,
            edge_id=edge_id,
            agent_id=self.agent_id,
            gateway_id=self.card.gateway_id,
            reserved_capacity_mbps=required_mbps,
            version=version,
        )
        self.resource_bindings[binding_id] = binding
        return binding

    def release_resource(self, binding_id: str) -> PhysicalResourceBinding | None:
        return self.resource_bindings.pop(binding_id, None)

    def restore_resource(self, binding: PhysicalResourceBinding) -> None:
        if not self.can_allocate(binding.reserved_capacity_mbps, binding.binding_id):
            raise ValueError(f"cannot restore physical binding {binding.binding_id}")
        self.resource_bindings[binding.binding_id] = binding

    def report_state(self) -> PhysicalAgentState:
        values = dict(self.card.state.values)
        total = self.total_capacity_mbps
        reserved = total - self.available_capacity_mbps
        signal = float(values.get("signal_quality", 0.98))
        reliability = float(values.get("reliability", 0.999))
        return PhysicalAgentState(
            agent_id=self.agent_id,
            gateway_id=self.card.gateway_id,
            signal_quality=min(1.0, max(0.0, signal)),
            available_capacity_mbps=self.available_capacity_mbps,
            resource_utilization=min(1.0, reserved / max(total, 1e-9)),
            reliability=min(1.0, max(0.0, reliability)),
            online=self.card.online,
            total_capacity_mbps=total,
            binding_ids=tuple(sorted(self.resource_bindings)),
        )

    def projected_state_without(self, binding_ids: set[str]) -> PhysicalAgentState:
        total = self.total_capacity_mbps
        reserved = sum(
            binding.reserved_capacity_mbps
            for binding_id, binding in self.resource_bindings.items()
            if binding.active and binding_id not in binding_ids
        )
        current = self.report_state()
        return replace(
            current,
            available_capacity_mbps=max(0.0, total - reserved),
            resource_utilization=min(1.0, reserved / max(total, 1e-9)),
            binding_ids=tuple(
                sorted(binding_id for binding_id in self.resource_bindings if binding_id not in binding_ids)
            ),
        )


def physical_binding_id(task_id: str, edge_id: str, agent_id: str) -> str:
    return f"{task_id}:{edge_id}:{agent_id}:physical"


# Preserve the old import name while upgrading its behavior.
PhyAgentStub = PhyAgent
