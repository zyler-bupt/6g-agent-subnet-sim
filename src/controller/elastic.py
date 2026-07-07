from __future__ import annotations

from dataclasses import dataclass, field

from src.controller.cost import CostModel
from src.core.models import AgentAction, SessionSpec, TaskSubnet


@dataclass(frozen=True)
class AdjustmentResult:
    strategy: str
    actions: tuple[AgentAction, ...]
    changed_sessions: tuple[SessionSpec, ...]
    changed_agents: int
    changed_edges: int
    changed_gateways: int
    service_interruption_ms: float
    operations: dict[str, int] = field(default_factory=dict)


def _active_actions(actions: list[AgentAction]) -> int:
    """Count Agents that actually changed something (non no-op actions)."""
    return sum(1 for action in actions if action.expected_effect)


class ElasticAdjuster:
    def __init__(self, cost_model: CostModel | None = None) -> None:
        self.cost_model = cost_model or CostModel()

    def choose_minimal_adjustment(
        self,
        subnet: TaskSubnet,
        risk_before: float,
        risk_after_local_actions: float,
    ) -> AdjustmentResult:
        tuned = _active_actions(subnet.actions)

        # Tier 1: local parameter tuning was enough to bring risk back in budget.
        if risk_after_local_actions <= subnet.task.qos.risk_threshold:
            operations = {"local_tune": tuned}
            return AdjustmentResult(
                strategy="local_tuning",
                actions=tuple(subnet.actions),
                changed_sessions=(),
                changed_agents=0,
                changed_edges=0,
                changed_gateways=0,
                service_interruption_ms=self.cost_model.interruption_ms(operations),
                operations=operations,
            )

        # Tier 2 (current): retune the support sessions in the affected range only.
        changed = tuple(
            SessionSpec(
                session_id=session.session_id,
                task_id=session.task_id,
                source=session.source,
                target=session.target,
                t_agent_id=session.t_agent_id,
                n_agent_id=session.n_agent_id,
                source_gateway=session.source_gateway,
                target_gateway=session.target_gateway,
                latency_budget_ms=session.latency_budget_ms,
                data_rate_mbps=session.data_rate_mbps * 0.9,
                path_id=session.path_id,
                gateway_path=session.gateway_path,
                status="retuned",
            )
            for session in subnet.sessions
        )
        gateways = {
            gateway
            for session in changed
            for gateway in (session.gateway_path or (session.source_gateway, session.target_gateway))
        }
        operations = {
            "local_tune": tuned,
            "session_setup": len(changed),
            "gateway_install": len(gateways),
        }
        return AdjustmentResult(
            strategy="support_session_retune",
            actions=tuple(subnet.actions),
            changed_sessions=changed,
            changed_agents=0,
            changed_edges=len(changed),
            changed_gateways=len(gateways),
            service_interruption_ms=self.cost_model.interruption_ms(operations),
            operations=operations,
        )
