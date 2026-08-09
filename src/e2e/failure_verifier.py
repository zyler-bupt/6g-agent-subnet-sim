from __future__ import annotations

from dataclasses import dataclass

from src.core.failures import FaultContext
from src.core.models import SessionSpec, TaskSubnet
from src.e2e.models import EdgeVerifyResult, VerifyResult


@dataclass
class FaultAwareTaskSubnetVerifier:
    """Method-independent verifier evaluated against the true fault state."""

    context: FaultContext
    mode: str = "in_memory_fault_aware"
    invocations: int = 0

    async def verify(self, subnet: TaskSubnet) -> VerifyResult:
        self.invocations += 1
        sessions = tuple(subnet.sessions)
        link_demand = sum(
            session.data_rate_mbps
            for session in sessions
            if self._uses_failed_link(session)
        )
        physical_demand: dict[str, float] = {}
        for session in sessions:
            for agent_id in session.p_agent_ids:
                physical_demand[agent_id] = (
                    physical_demand.get(agent_id, 0.0) + session.data_rate_mbps
                )
        results = tuple(
            self._verify_session(subnet, session, link_demand, physical_demand)
            for session in sessions
        )
        passed = sum(item.ok for item in results)
        return VerifyResult(
            ok=bool(results) and passed == len(results),
            verify_ms=0.0,
            checked_edges=len(results),
            passed_edges=passed,
            edge_results=results,
            mode=self.mode,
        )

    def _verify_session(
        self,
        subnet: TaskSubnet,
        session: SessionSpec,
        link_demand: float,
        physical_demand: dict[str, float],
    ) -> EdgeVerifyResult:
        edge = subnet.business_edges[session.business_edge_id]
        failures: list[str] = []
        if self.context.failed_agent_id and self.context.failed_agent_id in {
            session.source,
            session.target,
            session.t_agent_id,
            session.n_agent_id,
            *session.p_agent_ids,
        }:
            failures.append("failed_agent_still_selected")

        uses_failed_link = self._uses_failed_link(session)
        if uses_failed_link:
            link_capacity = self.context.degraded_link_capacity_mbps
            if self.context.fault_type == "LINK_FAILURE":
                link_capacity = 0.0
            if link_demand > link_capacity + 1e-9:
                failures.append("failed_or_overloaded_link")
        else:
            link_capacity = max(250.0, session.data_rate_mbps)

        physical_capacity = float("inf")
        for agent_id in session.p_agent_ids:
            state = subnet.physical_agents.get(agent_id)
            capacity = state.total_capacity_mbps if state is not None else 0.0
            if agent_id == self.context.physical_agent_id:
                capacity = self.context.physical_capacity_mbps
            physical_capacity = min(physical_capacity, capacity)
            if physical_demand.get(agent_id, 0.0) > capacity + 1e-9:
                failures.append(f"physical_capacity_exceeded:{agent_id}")
            if state is None or not state.online:
                failures.append(f"physical_unavailable:{agent_id}")
        if physical_capacity == float("inf"):
            physical_capacity = 0.0

        reachable = not failures
        throughput = (
            min(session.data_rate_mbps, link_capacity, physical_capacity)
            if reachable
            else 0.0
        )
        hop_latency = max(0, len(session.gateway_path) - 1) * 4.0
        degradation_latency = (
            (1.0 - self.context.severity) * 70.0 if uses_failed_link else 0.0
        )
        latency = 5.0 + hop_latency + degradation_latency
        loss = min(1.0, (1.0 - self.context.severity) * 0.08) if uses_failed_link else 0.001
        reliability = 1.0 - loss
        max_loss = edge.max_loss_rate if edge.max_loss_rate is not None else subnet.task.qos.max_loss_rate
        minimum_reliability = (
            edge.min_reliability
            if edge.min_reliability is not None
            else subnet.task.qos.min_reliability
        )
        if latency > edge.latency_budget_ms + 1e-9:
            failures.append("latency_qos")
        if loss > max_loss + 1e-9:
            failures.append("loss_qos")
        if reliability + 1e-9 < minimum_reliability:
            failures.append("reliability_qos")
        if throughput + 1e-9 < session.data_rate_mbps:
            failures.append("throughput_qos")
        ok = not failures
        return EdgeVerifyResult(
            session_id=session.session_id,
            source=session.source,
            target=session.target,
            ok=ok,
            reachable=reachable,
            latency_ms=latency,
            max_latency_ms=edge.latency_budget_ms,
            loss_rate=loss,
            max_loss_rate=max_loss,
            available_bandwidth_mbps=min(link_capacity, physical_capacity),
            min_bandwidth_mbps=session.data_rate_mbps,
            throughput_mbps=throughput,
            error=";".join(dict.fromkeys(failures)),
        )

    def _uses_failed_link(self, session: SessionSpec) -> bool:
        source, target = self.context.failed_link
        if not source or not target:
            return False
        return (source, target) in set(zip(session.gateway_path, session.gateway_path[1:]))


@dataclass
class StructuralTaskSubnetVerifier:
    """Ablation verifier: connectivity structure only, no fault/QoS oracle."""

    mode: str = "in_memory_structural_only"

    async def verify(self, subnet: TaskSubnet) -> VerifyResult:
        results = tuple(
            EdgeVerifyResult(
                session_id=session.session_id,
                source=session.source,
                target=session.target,
                ok=True,
                reachable=True,
                latency_ms=0.0,
                max_latency_ms=session.latency_budget_ms,
                loss_rate=0.0,
                max_loss_rate=subnet.task.qos.max_loss_rate,
                available_bandwidth_mbps=session.data_rate_mbps,
                min_bandwidth_mbps=session.data_rate_mbps,
                throughput_mbps=session.data_rate_mbps,
            )
            for session in subnet.sessions
        )
        return VerifyResult(
            ok=bool(results),
            verify_ms=0.0,
            checked_edges=len(results),
            passed_edges=len(results),
            edge_results=results,
            mode=self.mode,
        )
