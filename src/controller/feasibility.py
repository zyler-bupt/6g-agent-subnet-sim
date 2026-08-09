from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

from src.controller.conflicts import detect_write_set_conflicts
from src.core.cross_layer import (
    CrossLayerTaskState,
    LayerProposal,
)
from src.core.models import TaskSubnet


@dataclass(frozen=True)
class FourLayerFeasibilityResult:
    feasible: bool
    violations: tuple[str, ...] = field(default_factory=tuple)
    checked_edges: int = 0
    checked_physical_bindings: int = 0


@dataclass(frozen=True)
class CrossLayerEdgeMetrics:
    edge_id: str
    application_rate_mbps: float
    transport_rate_mbps: float
    network_bandwidth_mbps: float
    physical_capacity_mbps: float
    latency_ms: float
    packet_loss_rate: float
    reliability: float
    throughput_mbps: float
    qos_satisfied: bool


@dataclass(frozen=True)
class CrossLayerFeasibilityResult:
    feasible: bool
    projected_state: CrossLayerTaskState
    violations: tuple[str, ...] = field(default_factory=tuple)
    edge_metrics: dict[str, CrossLayerEdgeMetrics] = field(default_factory=dict)
    shared_resource_demand_mbps: dict[str, float] = field(default_factory=dict)
    write_conflict_ids: tuple[str, ...] = field(default_factory=tuple)
    resource_cost: float = 0.0
    change_cost: float = 0.0


def check_four_layer_feasibility(subnet: TaskSubnet) -> FourLayerFeasibilityResult:
    violations: list[str] = []
    sessions_by_edge = {
        session.business_edge_id: session
        for session in subnet.sessions
    }

    for edge in subnet.task.biz_edges:
        source = subnet.application_agents.get(edge.source)
        target = subnet.application_agents.get(edge.target)
        if source is None or not source.online:
            violations.append(f"application_source_missing:{edge.edge_id}")
        if target is None or not target.online:
            violations.append(f"application_target_missing:{edge.edge_id}")
        session = sessions_by_edge.get(edge.edge_id)
        if session is None:
            violations.append(f"transport_session_missing:{edge.edge_id}")
            continue

        transport = subnet.transport_agents.get(session.t_agent_id)
        if transport is None or not transport.online or not transport.connection_active:
            violations.append(f"transport_unavailable:{edge.edge_id}")
        network = subnet.network_agents.get(session.n_agent_id)
        if network is None or not network.online or not network.reachable:
            violations.append(f"network_unavailable:{edge.edge_id}")
        elif network.available_capacity_mbps + 1e-9 < edge.data_rate_mbps:
            violations.append(f"network_capacity_exceeded:{edge.edge_id}")

        if not session.p_agent_ids:
            violations.append(f"physical_agent_missing:{edge.edge_id}")
        required_reliability = (
            edge.min_reliability
            if edge.min_reliability is not None
            else subnet.task.qos.min_reliability
        )
        for p_agent_id in session.p_agent_ids:
            physical = subnet.physical_agents.get(p_agent_id)
            if physical is None or not physical.online:
                violations.append(f"physical_unavailable:{edge.edge_id}:{p_agent_id}")
                continue
            if physical.reliability + 1e-9 < required_reliability:
                violations.append(f"physical_reliability_low:{edge.edge_id}:{p_agent_id}")
            binding = next(
                (
                    item
                    for item in subnet.physical_bindings.values()
                    if item.edge_id == edge.edge_id and item.agent_id == p_agent_id
                ),
                None,
            )
            if binding is None:
                violations.append(f"physical_binding_missing:{edge.edge_id}:{p_agent_id}")
            elif binding.reserved_capacity_mbps + 1e-9 < edge.data_rate_mbps:
                violations.append(f"physical_reservation_insufficient:{edge.edge_id}:{p_agent_id}")
            elif binding.binding_id not in physical.binding_ids:
                violations.append(
                    f"physical_binding_state_mismatch:{edge.edge_id}:{p_agent_id}"
                )

    for network in subnet.network_agents.values():
        demand = sum(
            session.data_rate_mbps
            for session in subnet.sessions
            if session.n_agent_id == network.agent_id
        )
        if demand > network.available_capacity_mbps + 1e-9:
            violations.append(f"network_aggregate_capacity_exceeded:{network.agent_id}")

    for physical in subnet.physical_agents.values():
        reserved = sum(
            binding.reserved_capacity_mbps
            for binding in subnet.physical_bindings.values()
            if binding.agent_id == physical.agent_id and binding.active
        )
        if reserved > physical.total_capacity_mbps + 1e-9:
            violations.append(f"physical_capacity_exceeded:{physical.agent_id}")

    return FourLayerFeasibilityResult(
        feasible=not violations,
        violations=tuple(violations),
        checked_edges=len(subnet.task.biz_edges),
        checked_physical_bindings=len(subnet.physical_bindings),
    )


def evaluate_cross_layer_combination(
    state: CrossLayerTaskState,
    proposals: Iterable[LayerProposal] = (),
    *,
    check_write_sets: bool = True,
) -> CrossLayerFeasibilityResult:
    selected = tuple(proposals)
    projected = project_cross_layer_state(state, selected)
    violations: list[str] = []
    stable_version = state.metadata.get("stable_version")
    if stable_version is not None:
        violations.extend(
            f"stale_state:{proposal.proposal_id}"
            for proposal in selected
            if proposal.parameters.get("observed_version", stable_version)
            != stable_version
        )
    violations.extend(
        f"stale_state:{proposal_id}"
        for proposal_id in projected.metadata.get("executed_stale_proposals", ())
    )
    metrics: dict[str, CrossLayerEdgeMetrics] = {}
    background_demand = projected.metadata.get(
        "shared_resource_background_demand_mbps",
        {},
    )
    shared_demand: dict[str, float] = {
        resource_id: float(background_demand.get(resource_id, 0.0))
        for resource_id in projected.shared_resource_capacity_mbps
    }
    for edge_id, constraint in projected.constraints.items():
        application = projected.application[edge_id]
        transport = projected.transport[edge_id]
        network = projected.network[edge_id]
        physical = projected.physical[edge_id]
        if application.required_rate_mbps > transport.send_rate_mbps + 1e-9:
            violations.append(f"application_transport_rate:{edge_id}")
        if application.required_rate_mbps > min(
            network.available_bandwidth_mbps,
            physical.available_capacity_mbps,
        ) + 1e-9:
            violations.append(f"application_capacity:{edge_id}")
        if transport.send_rate_mbps > network.available_bandwidth_mbps + 1e-9:
            violations.append(f"transport_network_rate:{edge_id}")
        if network.available_bandwidth_mbps > physical.available_capacity_mbps + 1e-9:
            violations.append(f"network_physical_capacity:{edge_id}")
        if not network.reachable or not physical.online:
            violations.append(f"unreachable:{edge_id}")

        # Network utilization describes background load.  The selected
        # transport action contributes additional task load, so delay is a
        # joint consequence rather than a method-specific constant.
        offered_load = transport.send_rate_mbps / max(
            network.available_bandwidth_mbps,
            1e-9,
        )
        effective_utilization = network.utilization + 0.25 * offered_load
        queue_delay = (
            8.0
            * max(0.0, network.queue_occupancy)
            / max(0.05, 1.05 - effective_utilization)
        )
        latency = (
            transport.rtt_ms
            + network.latency_ms
            + physical.access_latency_ms
            + queue_delay
        )
        packet_loss = 1.0 - (
            (1.0 - min(1.0, max(0.0, transport.retransmission_rate)))
            * (1.0 - min(1.0, max(0.0, network.packet_loss_rate)))
            * min(1.0, max(0.0, physical.reliability))
        )
        reliability = (
            min(1.0, max(0.0, transport.reliability))
            * min(1.0, max(0.0, network.reliability))
            * min(1.0, max(0.0, physical.reliability))
        )
        throughput = min(
            transport.send_rate_mbps,
            network.available_bandwidth_mbps,
            physical.available_capacity_mbps,
        )
        if application.required_rate_mbps + 1e-9 < constraint.desired_rate_mbps:
            violations.append(f"application_quality_qos:{edge_id}")
        if latency > constraint.max_latency_ms + 1e-9:
            violations.append(f"latency_qos:{edge_id}")
        if packet_loss > constraint.max_loss_rate + 1e-9:
            violations.append(f"loss_qos:{edge_id}")
        if reliability + 1e-9 < constraint.min_reliability:
            violations.append(f"reliability_qos:{edge_id}")
        access_requirements = projected.metadata.get(
            "route_access_requirements",
            {},
        ).get(edge_id, {})
        required_access = access_requirements.get(network.selected_route)
        if required_access is not None and physical.access_id != required_access:
            violations.append(f"network_physical_access:{edge_id}")
        resource_id = constraint.shared_resource_id
        shared_demand[resource_id] = (
            shared_demand.get(resource_id, 0.0) + transport.send_rate_mbps
        )
        edge_violations = [
            item for item in violations if item.endswith(f":{edge_id}")
        ]
        metrics[edge_id] = CrossLayerEdgeMetrics(
            edge_id=edge_id,
            application_rate_mbps=application.required_rate_mbps,
            transport_rate_mbps=transport.send_rate_mbps,
            network_bandwidth_mbps=network.available_bandwidth_mbps,
            physical_capacity_mbps=physical.available_capacity_mbps,
            latency_ms=latency,
            packet_loss_rate=packet_loss,
            reliability=reliability,
            throughput_mbps=throughput,
            qos_satisfied=not edge_violations,
        )

    for resource_id, demand in shared_demand.items():
        capacity = projected.shared_resource_capacity_mbps.get(resource_id, 0.0)
        if demand > capacity + 1e-9:
            violations.append(f"shared_resource:{resource_id}")

    write_conflicts = (
        detect_write_set_conflicts(selected) if check_write_sets else ()
    )
    violations.extend(
        f"write_set:{record.conflict_id}" for record in write_conflicts
    )
    executed_write_conflicts = tuple(
        projected.metadata.get("executed_write_conflicts", ())
    )
    violations.extend(
        f"write_set:{conflict_id}" for conflict_id in executed_write_conflicts
    )
    # Update edge QoS flags after shared/write violations; affected edges must
    # not be reported healthy when their common resource or write set failed.
    if (
        write_conflicts
        or executed_write_conflicts
        or any(item.startswith("stale_state") for item in violations)
        or any(item.startswith("shared_resource") for item in violations)
    ):
        metrics = {
            edge_id: replace(item, qos_satisfied=False)
            for edge_id, item in metrics.items()
        }
    resource_cost = sum(shared_demand.values()) + sum(
        max(0.0, 1.0 - physical.signal_quality) * 10.0
        for physical in projected.physical.values()
    )
    change_cost = sum(0 if proposal.is_keep else 1 for proposal in selected)
    return CrossLayerFeasibilityResult(
        feasible=not violations,
        projected_state=projected,
        violations=tuple(dict.fromkeys(violations)),
        edge_metrics=metrics,
        shared_resource_demand_mbps=shared_demand,
        write_conflict_ids=tuple(record.conflict_id for record in write_conflicts)
        + executed_write_conflicts,
        resource_cost=resource_cost,
        change_cost=float(change_cost),
    )


def project_cross_layer_state(
    state: CrossLayerTaskState,
    proposals: Iterable[LayerProposal],
) -> CrossLayerTaskState:
    application = dict(state.application)
    transport = dict(state.transport)
    network = dict(state.network)
    physical = dict(state.physical)
    selected = tuple(proposals)
    for proposal in sorted(selected, key=lambda item: _layer_order(item.layer)):
        for edge_id in proposal.affected_edges:
            parameters = proposal.parameters
            if proposal.layer == "application" and edge_id in application:
                current = application[edge_id]
                application[edge_id] = replace(
                    current,
                    required_rate_mbps=float(
                        parameters.get("application_rate_mbps", current.required_rate_mbps)
                    ),
                    quality_level=int(
                        parameters.get("quality_level", current.quality_level)
                    ),
                    priority=int(parameters.get("priority", current.priority)),
                )
            elif proposal.layer == "transport" and edge_id in transport:
                current = transport[edge_id]
                transport[edge_id] = replace(
                    current,
                    send_rate_mbps=float(
                        parameters.get("transport_rate_mbps", current.send_rate_mbps)
                    ),
                    congestion_window=float(
                        parameters.get("congestion_window", current.congestion_window)
                    ),
                    retransmission_rate=float(
                        parameters.get("retransmission_rate", current.retransmission_rate)
                    ),
                    rtt_ms=float(parameters.get("transport_rtt_ms", current.rtt_ms)),
                    multipath_enabled=bool(
                        parameters.get("multipath_enabled", current.multipath_enabled)
                    ),
                    reliability_mode=str(
                        parameters.get("reliability_mode", current.reliability_mode)
                    ),
                    reliability=float(
                        parameters.get("transport_reliability", current.reliability)
                    ),
                )
            elif proposal.layer == "network" and edge_id in network:
                current = network[edge_id]
                network[edge_id] = replace(
                    current,
                    route_id=str(parameters.get("route_id", current.route_id)),
                    selected_route=str(
                        parameters.get("selected_route", current.selected_route)
                    ),
                    available_bandwidth_mbps=float(
                        parameters.get(
                            "network_bandwidth_mbps",
                            current.available_bandwidth_mbps,
                        )
                    ),
                    utilization=float(
                        parameters.get("network_utilization", current.utilization)
                    ),
                    queue_occupancy=float(
                        parameters.get("queue_occupancy", current.queue_occupancy)
                    ),
                    latency_ms=float(
                        parameters.get("network_latency_ms", current.latency_ms)
                    ),
                    packet_loss_rate=float(
                        parameters.get("network_loss_rate", current.packet_loss_rate)
                    ),
                    reliability=float(
                        parameters.get("network_reliability", current.reliability)
                    ),
                    reachable=bool(parameters.get("reachable", current.reachable)),
                )
            elif proposal.layer == "physical" and edge_id in physical:
                current = physical[edge_id]
                physical[edge_id] = replace(
                    current,
                    access_id=str(parameters.get("access_id", current.access_id)),
                    signal_quality=float(
                        parameters.get("signal_quality", current.signal_quality)
                    ),
                    available_capacity_mbps=float(
                        parameters.get(
                            "physical_capacity_mbps",
                            current.available_capacity_mbps,
                        )
                    ),
                    resource_utilization=float(
                        parameters.get(
                            "physical_utilization",
                            current.resource_utilization,
                        )
                    ),
                    reliability=float(
                        parameters.get("physical_reliability", current.reliability)
                    ),
                    online=bool(parameters.get("physical_online", current.online)),
                    access_latency_ms=float(
                        parameters.get("access_latency_ms", current.access_latency_ms)
                    ),
                )
    return replace(
        state,
        application=application,
        transport=transport,
        network=network,
        physical=physical,
        applied_proposal_ids=tuple(proposal.proposal_id for proposal in selected),
    )


def combination_objective(
    feasibility: CrossLayerFeasibilityResult,
    proposals: Iterable[LayerProposal],
) -> tuple[float, float, float, float]:
    selected = tuple(proposals)
    qos_penalty = float(len(feasibility.violations))
    resource = feasibility.resource_cost
    change = feasibility.change_cost
    overhead = sum(proposal.expected_cost for proposal in selected)
    benefit = sum(proposal.expected_qos_gain * proposal.confidence for proposal in selected)
    weighted_cost = overhead + 0.002 * resource + 0.15 * change - benefit
    return (qos_penalty, weighted_cost, change, resource)


def _layer_order(layer: str) -> int:
    return {"application": 0, "transport": 1, "network": 2, "physical": 3}.get(
        layer,
        99,
    )
