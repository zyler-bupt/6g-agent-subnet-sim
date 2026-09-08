from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import product
from typing import Iterable

from src.core.cross_layer import CrossLayerTaskState, LayerProposal


@dataclass(frozen=True)
class TruthEdgeMetrics:
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
class TruthEvaluation:
    feasible: bool
    projected_state: CrossLayerTaskState
    violations: tuple[str, ...] = field(default_factory=tuple)
    edge_metrics: dict[str, TruthEdgeMetrics] = field(default_factory=dict)
    shared_resource_demand_mbps: dict[str, float] = field(default_factory=dict)
    write_conflict_ids: tuple[str, ...] = field(default_factory=tuple)
    resource_cost: float = 0.0
    change_cost: float = 0.0


@dataclass(frozen=True)
class TruthEvaluatedCombination:
    proposal_ids: tuple[str, ...]
    feasible: bool
    violations: tuple[str, ...]
    objective: tuple[float, float, float, float]


@dataclass(frozen=True)
class TruthGroundTruthResult:
    ground_truth_conflict: bool
    ground_truth_resolvable: bool
    independent_proposal_ids: tuple[str, ...]
    independent_violations: tuple[str, ...]
    feasible_combinations: tuple[tuple[str, ...], ...]
    best_feasible_combination: tuple[str, ...]
    evaluated_combinations: tuple[TruthEvaluatedCombination, ...]
    local_proposal_feasibility: dict[str, bool]

    @property
    def candidate_combinations(self) -> int:
        return len(self.evaluated_combinations)


def evaluate_truth(
    state: CrossLayerTaskState,
    proposals: Iterable[LayerProposal] = (),
    *,
    check_write_sets: bool = True,
) -> TruthEvaluation:
    """Evaluate truth without importing the controller feasibility model."""

    selected = tuple(proposals)
    projected = _project_truth_state(state, selected)
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
        for proposal_id in projected.metadata.get(
            "executed_stale_proposals", ()
        )
    )

    background = projected.metadata.get(
        "shared_resource_background_demand_mbps", {}
    )
    shared_demand = {
        resource_id: float(background.get(resource_id, 0.0))
        for resource_id in projected.shared_resource_capacity_mbps
    }
    metrics: dict[str, TruthEdgeMetrics] = {}
    for edge_id, constraint in projected.constraints.items():
        edge_violations: list[str] = []
        application = projected.application[edge_id]
        transport = projected.transport[edge_id]
        network = projected.network[edge_id]
        physical = projected.physical[edge_id]
        transport_capacity = transport.admissible_capacity_mbps

        if application.required_rate_mbps > transport.send_rate_mbps + 1e-9:
            edge_violations.append(f"application_transport_rate:{edge_id}")
        if transport_capacity is not None:
            if application.required_rate_mbps > transport_capacity + 1e-9:
                edge_violations.append(
                    f"application_transport_capacity:{edge_id}"
                )
            if transport.send_rate_mbps > transport_capacity + 1e-9:
                edge_violations.append(
                    f"transport_admissible_capacity:{edge_id}"
                )
        if (
            application.required_rate_mbps
            > network.available_bandwidth_mbps + 1e-9
        ):
            edge_violations.append(f"application_network_capacity:{edge_id}")
        if transport.send_rate_mbps > network.available_bandwidth_mbps + 1e-9:
            edge_violations.append(f"transport_network_rate:{edge_id}")
        if (
            application.required_rate_mbps
            > physical.available_capacity_mbps + 1e-9
        ):
            edge_violations.append(f"application_physical_capacity:{edge_id}")
        if application.required_rate_mbps > min(
            network.available_bandwidth_mbps,
            physical.available_capacity_mbps,
        ) + 1e-9:
            edge_violations.append(f"application_capacity:{edge_id}")
        if transport.send_rate_mbps > physical.available_capacity_mbps + 1e-9:
            edge_violations.append(f"transport_physical_capacity:{edge_id}")
        if (
            not projected.metadata.get("independent_layer_capacities", False)
            and network.available_bandwidth_mbps
            > physical.available_capacity_mbps + 1e-9
        ):
            edge_violations.append(f"network_physical_capacity:{edge_id}")
        if not network.reachable or not physical.online:
            edge_violations.append(f"unreachable:{edge_id}")

        offered_load = transport.send_rate_mbps / max(
            network.available_bandwidth_mbps, 1e-9
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
            (1.0 - _unit_interval(transport.retransmission_rate))
            * (1.0 - _unit_interval(network.packet_loss_rate))
            * _unit_interval(physical.reliability)
        )
        reliability = (
            _unit_interval(transport.reliability)
            * _unit_interval(network.reliability)
            * _unit_interval(physical.reliability)
        )
        throughput = min(
            transport.send_rate_mbps,
            (
                transport.send_rate_mbps
                if transport_capacity is None
                else transport_capacity
            ),
            network.available_bandwidth_mbps,
            physical.available_capacity_mbps,
        )
        if application.required_rate_mbps + 1e-9 < constraint.desired_rate_mbps:
            edge_violations.append(f"application_quality_qos:{edge_id}")
        if latency > constraint.max_latency_ms + 1e-9:
            edge_violations.append(f"latency_qos:{edge_id}")
        if packet_loss > constraint.max_loss_rate + 1e-9:
            edge_violations.append(f"loss_qos:{edge_id}")
        if reliability + 1e-9 < constraint.min_reliability:
            edge_violations.append(f"reliability_qos:{edge_id}")
        access_requirements = projected.metadata.get(
            "route_access_requirements", {}
        ).get(edge_id, {})
        required_access = access_requirements.get(network.selected_route)
        if required_access is not None and physical.access_id != required_access:
            edge_violations.append(f"network_physical_access:{edge_id}")

        resource_id = constraint.shared_resource_id
        flow_demand = (
            application.required_rate_mbps
            if projected.metadata.get(
                "shared_demand_uses_application_rate", False
            )
            else transport.send_rate_mbps
        )
        shared_demand[resource_id] = (
            shared_demand.get(resource_id, 0.0) + flow_demand
        )
        violations.extend(edge_violations)
        metrics[edge_id] = TruthEdgeMetrics(
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

    shared_failure = False
    for resource_id, demand in shared_demand.items():
        capacity = projected.shared_resource_capacity_mbps.get(resource_id, 0.0)
        if demand > capacity + 1e-9:
            shared_failure = True
            violations.append(f"shared_resource:{resource_id}")

    write_conflicts = (
        _truth_write_conflicts(selected) if check_write_sets else ()
    )
    executed_write_conflicts = tuple(
        projected.metadata.get("executed_write_conflicts", ())
    )
    violations.extend(f"write_set:{value}" for value in write_conflicts)
    violations.extend(
        f"write_set:{value}" for value in executed_write_conflicts
    )
    if (
        write_conflicts
        or executed_write_conflicts
        or shared_failure
        or any(value.startswith("stale_state:") for value in violations)
    ):
        metrics = {
            edge_id: replace(value, qos_satisfied=False)
            for edge_id, value in metrics.items()
        }

    resource_cost = sum(shared_demand.values()) + sum(
        max(0.0, 1.0 - value.signal_quality) * 10.0
        for value in projected.physical.values()
    )
    unique_violations = tuple(dict.fromkeys(violations))
    return TruthEvaluation(
        feasible=not unique_violations,
        projected_state=projected,
        violations=unique_violations,
        edge_metrics=metrics,
        shared_resource_demand_mbps=shared_demand,
        write_conflict_ids=write_conflicts + executed_write_conflicts,
        resource_cost=resource_cost,
        change_cost=float(sum(not proposal.is_keep for proposal in selected)),
    )


def solve_truth(
    state: CrossLayerTaskState,
    proposals: Iterable[LayerProposal],
) -> TruthGroundTruthResult:
    selected_pool = tuple(proposals)
    groups = _proposal_groups(selected_pool)
    combinations = tuple(product(*(groups[key] for key in sorted(groups))))
    if not combinations:
        raise ValueError("truth solver requires proposals")

    evaluated: list[TruthEvaluatedCombination] = []
    evaluations: dict[tuple[str, ...], TruthEvaluation] = {}
    for raw_combination in combinations:
        combination = tuple(raw_combination)
        evaluation = evaluate_truth(state, combination)
        proposal_ids = tuple(value.proposal_id for value in combination)
        evaluations[proposal_ids] = evaluation
        changes = float(sum(not value.is_keep for value in combination))
        expected_cost = sum(value.expected_cost for value in combination)
        evaluated.append(
            TruthEvaluatedCombination(
                proposal_ids=proposal_ids,
                feasible=evaluation.feasible,
                violations=evaluation.violations,
                objective=(
                    float(len(evaluation.violations)),
                    float(expected_cost),
                    changes,
                    evaluation.resource_cost,
                ),
            )
        )

    keep_combination = tuple(
        next(value for value in groups[key] if value.is_keep)
        for key in sorted(groups)
    )
    keep_ids = tuple(value.proposal_id for value in keep_combination)
    keep_evaluation = evaluations[keep_ids]
    feasible = [value for value in evaluated if value.feasible]
    best_ids: tuple[str, ...] = ()
    if feasible:
        best_ids = min(
            feasible,
            key=lambda value: (
                value.objective[2],
                value.objective[1],
                value.proposal_ids,
            ),
        ).proposal_ids
    local_feasibility = {
        proposal.proposal_id: evaluate_truth(
            state, (proposal,), check_write_sets=False
        ).feasible
        for proposal in selected_pool
    }
    conflict = not keep_evaluation.feasible
    return TruthGroundTruthResult(
        ground_truth_conflict=conflict,
        ground_truth_resolvable=conflict and bool(feasible),
        independent_proposal_ids=keep_ids,
        independent_violations=keep_evaluation.violations,
        feasible_combinations=tuple(value.proposal_ids for value in feasible),
        best_feasible_combination=best_ids,
        evaluated_combinations=tuple(evaluated),
        local_proposal_feasibility=local_feasibility,
    )


def _project_truth_state(
    state: CrossLayerTaskState,
    proposals: tuple[LayerProposal, ...],
) -> CrossLayerTaskState:
    application = dict(state.application)
    transport = dict(state.transport)
    network = dict(state.network)
    physical = dict(state.physical)
    for proposal in sorted(proposals, key=lambda value: _layer_order(value.layer)):
        for edge_id in proposal.affected_edges:
            parameters = proposal.parameters
            if proposal.layer == "application" and edge_id in application:
                current = application[edge_id]
                application[edge_id] = replace(
                    current,
                    required_rate_mbps=float(
                        parameters.get(
                            "application_rate_mbps", current.required_rate_mbps
                        )
                    ),
                    quality_level=int(
                        parameters.get("quality_level", current.quality_level)
                    ),
                    priority=int(parameters.get("priority", current.priority)),
                )
            elif proposal.layer == "transport" and edge_id in transport:
                current = transport[edge_id]
                capacity = parameters.get(
                    "transport_admissible_capacity_mbps",
                    current.admissible_capacity_mbps,
                )
                transport[edge_id] = replace(
                    current,
                    send_rate_mbps=float(
                        parameters.get(
                            "transport_rate_mbps", current.send_rate_mbps
                        )
                    ),
                    congestion_window=float(
                        parameters.get(
                            "congestion_window", current.congestion_window
                        )
                    ),
                    retransmission_rate=float(
                        parameters.get(
                            "retransmission_rate", current.retransmission_rate
                        )
                    ),
                    rtt_ms=float(
                        parameters.get("transport_rtt_ms", current.rtt_ms)
                    ),
                    multipath_enabled=bool(
                        parameters.get(
                            "multipath_enabled", current.multipath_enabled
                        )
                    ),
                    reliability_mode=str(
                        parameters.get(
                            "reliability_mode", current.reliability_mode
                        )
                    ),
                    reliability=float(
                        parameters.get(
                            "transport_reliability", current.reliability
                        )
                    ),
                    admissible_capacity_mbps=(
                        None if capacity is None else float(capacity)
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
                        parameters.get(
                            "network_utilization", current.utilization
                        )
                    ),
                    queue_occupancy=float(
                        parameters.get("queue_occupancy", current.queue_occupancy)
                    ),
                    latency_ms=float(
                        parameters.get("network_latency_ms", current.latency_ms)
                    ),
                    packet_loss_rate=float(
                        parameters.get(
                            "network_loss_rate", current.packet_loss_rate
                        )
                    ),
                    reliability=float(
                        parameters.get(
                            "network_reliability", current.reliability
                        )
                    ),
                    reachable=bool(
                        parameters.get("reachable", current.reachable)
                    ),
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
                            "physical_utilization", current.resource_utilization
                        )
                    ),
                    reliability=float(
                        parameters.get(
                            "physical_reliability", current.reliability
                        )
                    ),
                    online=bool(
                        parameters.get("physical_online", current.online)
                    ),
                    access_latency_ms=float(
                        parameters.get(
                            "access_latency_ms", current.access_latency_ms
                        )
                    ),
                )
    return replace(
        state,
        application=application,
        transport=transport,
        network=network,
        physical=physical,
        applied_proposal_ids=tuple(value.proposal_id for value in proposals),
    )


def _proposal_groups(
    proposals: tuple[LayerProposal, ...],
) -> dict[tuple[str, str], tuple[LayerProposal, ...]]:
    mutable: dict[tuple[str, str], list[LayerProposal]] = {}
    for proposal in proposals:
        for edge_id in proposal.affected_edges:
            mutable.setdefault((edge_id, proposal.layer), []).append(proposal)
    if not mutable:
        raise ValueError("truth solver requires grouped proposals")
    edges = {edge_id for edge_id, _layer in mutable}
    required_layers = {"application", "transport", "network", "physical"}
    for edge_id in edges:
        present = {
            layer for candidate_edge, layer in mutable if candidate_edge == edge_id
        }
        if present != required_layers:
            raise ValueError(
                f"truth proposal pool for {edge_id} has layers {sorted(present)}"
            )
    return {
        key: tuple(sorted(values, key=lambda value: value.proposal_id))
        for key, values in mutable.items()
    }


def _truth_write_conflicts(
    proposals: tuple[LayerProposal, ...],
) -> tuple[str, ...]:
    conflicts: list[str] = []
    for index, left in enumerate(proposals):
        for right in proposals[index + 1 :]:
            overlap = left.write_set & right.write_set
            if not overlap or left.is_keep or right.is_keep:
                continue
            left_values = left.parameters.get("write_values", {})
            right_values = right.parameters.get("write_values", {})
            compatible = all(
                key in left_values
                and key in right_values
                and left_values[key] == right_values[key]
                for key in overlap
            )
            if compatible:
                continue
            conflicts.append(f"write:{left.proposal_id}:{right.proposal_id}")
    return tuple(conflicts)


def _unit_interval(value: float) -> float:
    return min(1.0, max(0.0, value))


def _layer_order(layer: str) -> int:
    return {"application": 0, "transport": 1, "network": 2, "physical": 3}.get(
        layer, 99
    )
