from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.controller.ground_truth import (
    PaperFeasibilityOracleResult,
    solve_global_layer_oracle,
)
from src.core.cross_layer import (
    ApplicationLayerState,
    CrossLayerTaskState,
    EdgeQoSConstraint,
    LayerProposal,
    NetworkLayerState,
    PhysicalLayerState,
    TransportLayerState,
)
from src.core.models import TaskSpec, to_jsonable
from src.sim.topology import TopologyCatalog
from src.simulation.scenario_generator import (
    ElasticScenarioGenerator,
    ScenarioConfig,
    ScenarioSnapshot,
)


class PaperConflictType(str, Enum):
    APPLICATION_NETWORK = "application_network"
    TRANSPORT_NETWORK = "transport_network"
    NETWORK_PHYSICAL = "network_physical"
    CASCADED = "cascaded_multi_layer"


_TYPE_CYCLE = (
    *(PaperConflictType.APPLICATION_NETWORK for _ in range(5)),
    *(PaperConflictType.TRANSPORT_NETWORK for _ in range(4)),
    *(PaperConflictType.NETWORK_PHYSICAL for _ in range(4)),
    *(PaperConflictType.CASCADED for _ in range(7)),
)


@dataclass(frozen=True)
class PaperConflictSnapshot:
    seed: int
    event_id: int
    conflict_density: float
    base: ScenarioSnapshot
    conflicted_edge_ids: tuple[str, ...]
    conflict_types: dict[str, PaperConflictType]
    state: CrossLayerTaskState
    proposals: tuple[LayerProposal, ...]
    oracle: PaperFeasibilityOracleResult
    topology_fingerprint: str
    scenario_fingerprint: str
    qos_fingerprint: str
    event_fingerprint: str

    @property
    def task(self) -> TaskSpec:
        return self.base.task

    @property
    def catalog(self) -> TopologyCatalog:
        return self.base.catalog

    @property
    def proposal_fingerprint(self) -> str:
        return _fingerprint(self.proposals)


def solve_paper_feasibility_oracle(
    state: CrossLayerTaskState,
    proposals: tuple[LayerProposal, ...],
) -> PaperFeasibilityOracleResult:
    """Return the exact label without accepting or observing a method name."""

    return solve_global_layer_oracle(state, proposals)


def generate_conflict_snapshot(
    conflict_density_percent: int | float,
    seed: int,
    event_id: int,
) -> PaperConflictSnapshot:
    density_percent = float(conflict_density_percent)
    if density_percent not in {0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0}:
        raise ValueError("unsupported paper conflict density")
    density = density_percent / 100.0
    base = ElasticScenarioGenerator().generate(
        ScenarioConfig(
            num_agents=20,
            edge_ratio=1.5,
            num_gateways=10,
            cross_gateway_edge_ratio=0.65,
            agent_removal_ratio=0.10,
            removed_agent_type="leaf",
            multi_hop=True,
        ),
        seed,
    )
    edges = tuple(sorted(base.task.biz_edges, key=lambda item: item.edge_id))
    conflict_count = round(density * len(edges))
    rng = random.Random(f"paper-exp2:{density_percent:g}:{seed}:{event_id}")
    shuffled_edges = list(edges)
    rng.shuffle(shuffled_edges)
    conflicted = tuple(sorted(edge.edge_id for edge in shuffled_edges[:conflict_count]))

    type_cycle = list(_TYPE_CYCLE)
    rng.shuffle(type_cycle)
    conflict_types = {
        edge_id: type_cycle[index % len(type_cycle)]
        for index, edge_id in enumerate(conflicted)
    }
    state = _build_state(
        base,
        conflict_types,
        density,
        seed=seed,
        event_id=event_id,
    )
    proposals = _build_global_proposals(base, state, conflicted, density)
    oracle = solve_paper_feasibility_oracle(state, proposals)
    scenario_payload = {
        "base": base.fingerprint,
        "density": density,
        "conflict_types": conflict_types,
        "state": state,
        "proposals": proposals,
    }
    return PaperConflictSnapshot(
        seed=seed,
        event_id=event_id,
        conflict_density=density,
        base=base,
        conflicted_edge_ids=conflicted,
        conflict_types=conflict_types,
        state=state,
        proposals=proposals,
        oracle=oracle,
        topology_fingerprint=base.fingerprint,
        scenario_fingerprint=_fingerprint(scenario_payload),
        qos_fingerprint=_fingerprint(state.constraints),
        event_fingerprint=_fingerprint(
            {
                "seed": seed,
                "event_id": event_id,
                "density": density,
                "conflicts": conflict_types,
            }
        ),
    )


def _build_state(
    base: ScenarioSnapshot,
    conflict_types: dict[str, PaperConflictType],
    density: float,
    *,
    seed: int,
    event_id: int,
) -> CrossLayerTaskState:
    application: dict[str, ApplicationLayerState] = {}
    transport: dict[str, TransportLayerState] = {}
    network: dict[str, NetworkLayerState] = {}
    physical: dict[str, PhysicalLayerState] = {}
    constraints: dict[str, EdgeQoSConstraint] = {}
    shared_capacity: dict[str, float] = {}
    cascaded_shared_pressure = _cascaded_shared_pressure(density, seed, event_id)
    hidden_route_coupling = _hidden_route_coupling(density, seed, event_id)
    route_access_requirements: dict[str, dict[str, str]] = {}
    gateway_by_agent = {
        spec.agent_id: spec.gateway_id
        for spec in base.catalog.agents
        if spec.agent_id in base.task.app_agents
    }
    edge_gateways: dict[str, tuple[str, ...]] = {}
    for edge in base.task.biz_edges:
        edge_id = edge.edge_id
        kind = conflict_types.get(edge_id)
        edge_rng = random.Random(f"paper-exp2-edge:{seed}:{event_id}:{edge_id}")
        values = _edge_values(kind, density, edge_rng.uniform(-0.8, 0.8))
        resource_id = f"paper-exp2-resource:{edge_id}"
        application[edge_id] = ApplicationLayerState(
            task_id=base.task.task_id,
            stage="cross_layer_coordination",
            required_rate_mbps=values["application_rate"],
            max_latency_ms=45.0,
            max_loss_rate=0.05,
            min_reliability=0.98,
            priority=edge.priority,
            quality_level=2,
        )
        transport[edge_id] = TransportLayerState(
            session_id=f"{base.task.task_id}:{edge_id}:session",
            send_rate_mbps=values["transport_rate"],
            congestion_window=32.0,
            retransmission_rate=0.001,
            rtt_ms=4.0,
            multipath_enabled=False,
            reliability_mode="balanced",
            reliability=0.9995,
            admissible_capacity_mbps=values["transport_capacity"],
        )
        network[edge_id] = NetworkLayerState(
            route_id=f"{edge_id}:primary",
            available_bandwidth_mbps=values["network_bandwidth"],
            utilization=values["network_utilization"],
            queue_occupancy=values["queue_occupancy"],
            latency_ms=8.0,
            packet_loss_rate=0.001,
            reachable=True,
            candidate_routes=("primary", "reserved", "alternate"),
            selected_route="primary",
            reliability=0.9995,
        )
        physical[edge_id] = PhysicalLayerState(
            access_id="paper-primary-access",
            signal_quality=values["signal_quality"],
            available_capacity_mbps=values["physical_capacity"],
            resource_utilization=values["physical_utilization"],
            reliability=0.9995,
            online=True,
            access_latency_ms=2.0,
        )
        constraints[edge_id] = EdgeQoSConstraint(
            edge_id=edge_id,
            max_latency_ms=45.0,
            max_loss_rate=0.05,
            min_reliability=0.98,
            shared_resource_id=resource_id,
            desired_rate_mbps=values["desired_rate"],
        )
        shared_capacity[resource_id] = (
            9.0
            if kind == PaperConflictType.CASCADED and cascaded_shared_pressure
            else 40.0
        )
        if kind == PaperConflictType.CASCADED and hidden_route_coupling:
            route_access_requirements[edge_id] = {
                "primary": "paper-primary-access",
                "alternate": "paper-alternate-access",
            }
        source_gateway = gateway_by_agent[edge.source]
        target_gateway = gateway_by_agent[edge.target]
        edge_gateways[edge_id] = base.gateway_paths[(source_gateway, target_gateway)]

    if conflict_types and _inject_infeasible_stress(density, seed, event_id):
        first_edge = min(conflict_types)
        current = constraints[first_edge]
        constraints[first_edge] = EdgeQoSConstraint(
            edge_id=current.edge_id,
            max_latency_ms=current.max_latency_ms,
            max_loss_rate=current.max_loss_rate,
            min_reliability=current.min_reliability,
            shared_resource_id=current.shared_resource_id,
            desired_rate_mbps=21.0,
        )

    return CrossLayerTaskState(
        task_id=base.task.task_id,
        scenario="paper_conflict_density",
        pressure=density,
        application=application,
        transport=transport,
        network=network,
        physical=physical,
        constraints=constraints,
        shared_resource_capacity_mbps=shared_capacity,
        metadata={
            "stable_version": 1,
            "edge_gateways": edge_gateways,
            "paper_conflict_types": {
                edge_id: kind.value for edge_id, kind in conflict_types.items()
            },
            "paper_conflicted_edge_ids": tuple(sorted(conflict_types)),
            "paper_candidate_space": "three_global_actions_per_layer",
            "cascaded_shared_pressure": cascaded_shared_pressure,
            "shared_demand_uses_application_rate": True,
            "route_access_requirements": route_access_requirements,
            "hidden_route_coupling": hidden_route_coupling,
        },
    )


def _edge_values(
    kind: PaperConflictType | None,
    density: float,
    jitter: float,
) -> dict[str, float]:
    stable = {
        "application_rate": 8.0,
        "transport_rate": 8.0,
        "transport_capacity": 12.0,
        "network_bandwidth": 12.0,
        "network_utilization": 0.25,
        "queue_occupancy": 0.06,
        "physical_capacity": 12.0,
        "physical_utilization": 0.35,
        "signal_quality": 0.95,
        "desired_rate": 8.0,
    }
    if kind is None:
        return stable
    if kind == PaperConflictType.APPLICATION_NETWORK:
        return {
            **stable,
            "application_rate": 12.0,
            "transport_rate": 12.0,
            "transport_capacity": 14.0,
            "network_bandwidth": 9.0,
            "network_utilization": 0.65,
            "queue_occupancy": 0.20,
            "physical_capacity": 18.0,
        }
    if kind == PaperConflictType.TRANSPORT_NETWORK:
        return {
            **stable,
            "transport_rate": 14.0,
            "transport_capacity": 14.0,
            "network_bandwidth": 10.0,
            "network_utilization": 0.82,
            "queue_occupancy": 0.35,
            "physical_capacity": 18.0,
        }
    if kind == PaperConflictType.NETWORK_PHYSICAL:
        return {
            **stable,
            "network_bandwidth": 16.0,
            "physical_capacity": 10.0,
            "physical_utilization": 0.65,
            "signal_quality": 0.82,
        }
    cascaded_rate = 12.0 + 7.0 * density + jitter
    return {
        **stable,
        "application_rate": cascaded_rate,
        "transport_rate": cascaded_rate + 1.0,
        "transport_capacity": 16.0,
        "network_bandwidth": 10.0,
        "network_utilization": 0.78,
        "queue_occupancy": 0.30,
        "physical_capacity": 9.0,
        "physical_utilization": 0.88,
        "signal_quality": 0.72,
    }


def _inject_infeasible_stress(density: float, seed: int, event_id: int) -> bool:
    density_index = int(round(density * 10.0))
    if density_index <= 0:
        return False
    schedule_index = (density_index - 1) * 10 + seed * 2 + event_id
    return schedule_index % 8 == 0


def _cascaded_shared_pressure(density: float, seed: int, event_id: int) -> bool:
    trial_index = seed * 2 + event_id
    if density >= 0.60:
        return trial_index % 5 != 0
    if density >= 0.50:
        return trial_index % 2 == 0
    return False


def _hidden_route_coupling(density: float, seed: int, event_id: int) -> bool:
    trial_index = seed * 2 + event_id
    if density >= 0.60:
        return trial_index % 5 in {0, 1}
    if density >= 0.50:
        return trial_index % 5 == 0
    return False


def _build_global_proposals(
    base: ScenarioSnapshot,
    state: CrossLayerTaskState,
    conflicted_edge_ids: tuple[str, ...],
    density: float,
) -> tuple[LayerProposal, ...]:
    affected = frozenset(conflicted_edge_ids)
    affected_sessions = frozenset(
        state.transport[edge_id].session_id for edge_id in conflicted_edge_ids
    )
    affected_routes = frozenset(
        state.network[edge_id].route_id for edge_id in conflicted_edge_ids
    )
    affected_gateways = frozenset(
        gateway
        for edge_id in conflicted_edge_ids
        for gateway in state.metadata["edge_gateways"][edge_id]
    )
    profiles: dict[str, tuple[tuple[str, float, float, dict[str, Any]], ...]] = {
        "application": (
            ("KEEP_QUALITY", 0.0, 0.0, {}),
            ("CHANGE_PRIORITY", 0.20, 0.12, {"application_rate_mbps": 10.0, "priority": 3}),
            ("DECREASE_APPLICATION_RATE", 0.32, 0.22, {"application_rate_mbps": 8.0, "quality_level": 1}),
        ),
        "transport": (
            ("KEEP_TRANSPORT", 0.0, 0.0, {}),
            ("DECREASE_SEND_RATE", 0.20, 0.12, {"transport_rate_mbps": 10.0, "transport_admissible_capacity_mbps": 20.0}),
            ("RESERVE_TRANSPORT_SERVICE", 0.32, 0.20, {"transport_rate_mbps": 8.0, "transport_admissible_capacity_mbps": 20.0, "multipath_enabled": True}),
        ),
        "network": (
            ("KEEP_ROUTE", 0.0, 0.0, {}),
            ("RESERVE_BANDWIDTH", 0.30, 0.14, {"network_bandwidth_mbps": 16.0, "network_utilization": 0.30, "queue_occupancy": 0.08}),
            ("SWITCH_ROUTE", 0.40, 0.28, {"route_id": "paper-alternate-route", "selected_route": "alternate", "network_bandwidth_mbps": 16.0, "network_utilization": 0.24, "queue_occupancy": 0.05, "network_latency_ms": 11.0}),
        ),
        "physical": (
            ("KEEP_RESOURCE", 0.0, 0.0, {}),
            ("REALLOCATE_RESOURCE", 0.30, 0.16, {"access_id": "paper-alternate-access", "physical_capacity_mbps": 16.0, "physical_utilization": 0.40, "signal_quality": 0.93}),
            ("ALLOCATE_RESOURCE", 0.40, 0.30, {"access_id": "paper-alternate-access", "physical_capacity_mbps": 16.0, "physical_utilization": 0.48, "signal_quality": 0.96}),
        ),
    }
    proposals: list[LayerProposal] = []
    for layer in ("application", "transport", "network", "physical"):
        for index, (action, gain, cost, parameters) in enumerate(profiles[layer]):
            proposals.append(
                LayerProposal(
                    proposal_id=f"{base.task.task_id}:paper:{layer}:{index}:{action}",
                    task_id=base.task.task_id,
                    layer=layer,
                    action=action,
                    target_objects=frozenset(f"edge:{edge_id}" for edge_id in affected),
                    read_set=frozenset({f"{layer}:paper:state", "task:paper:qos"}),
                    write_set=(
                        frozenset()
                        if action.startswith("KEEP_")
                        else frozenset({f"{layer}:paper:configuration"})
                    ),
                    expected_qos_gain=gain * max(density, 0.10),
                    expected_cost=cost,
                    confidence=0.95,
                    required_bandwidth_mbps=8.0,
                    required_physical_capacity_mbps=8.0,
                    expected_latency_ms=12.0,
                    expected_loss_rate=0.01,
                    affected_edges=affected,
                    affected_sessions=affected_sessions,
                    affected_routes=affected_routes,
                    affected_gateways=affected_gateways,
                    parameters={**parameters, "observed_version": 1},
                )
            )
    return tuple(proposals)


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
