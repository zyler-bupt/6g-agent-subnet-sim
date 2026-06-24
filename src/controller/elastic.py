from __future__ import annotations

from dataclasses import dataclass

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


class ElasticAdjuster:
    def choose_minimal_adjustment(
        self,
        subnet: TaskSubnet,
        risk_before: float,
        risk_after_local_actions: float,
    ) -> AdjustmentResult:
        if risk_after_local_actions <= subnet.task.qos.risk_threshold:
            return AdjustmentResult(
                strategy="local_tuning",
                actions=tuple(subnet.actions),
                changed_sessions=(),
                changed_agents=0,
                changed_edges=0,
                changed_gateways=0,
                service_interruption_ms=5.0,
            )

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
                status="retuned",
            )
            for session in subnet.sessions
        )
        gateways = {gateway for session in changed for gateway in (session.source_gateway, session.target_gateway)}
        return AdjustmentResult(
            strategy="support_session_retune",
            actions=tuple(subnet.actions),
            changed_sessions=changed,
            changed_agents=0,
            changed_edges=len(changed),
            changed_gateways=len(gateways),
            service_interruption_ms=18.0 + 2.0 * len(changed),
        )

