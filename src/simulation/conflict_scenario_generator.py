from __future__ import annotations

import hashlib
import json
import random
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import Any

from src.agents.layer_proposals import collect_layer_proposals
from src.controller.ground_truth import GroundTruthResult, GroundTruthSolver
from src.core.cross_layer import (
    ApplicationLayerState,
    CrossLayerTaskState,
    EdgeQoSConstraint,
    LayerProposal,
    NetworkLayerState,
    PhysicalLayerState,
    TransportLayerState,
)
from src.core.models import to_jsonable
from src.simulation.scenario_generator import (
    ElasticScenarioGenerator,
    ScenarioConfig,
    ScenarioSnapshot,
)


MAIN_SCENARIOS = (
    "application_capacity",
    "transport_network",
    "network_physical",
)


@dataclass(frozen=True)
class ConflictScenarioConfig:
    num_agents: int = 10
    edge_ratio: float = 1.5
    num_gateways: int = 4
    cross_gateway_edge_ratio: float = 0.5
    multi_hop: bool = True


@dataclass(frozen=True)
class ConflictScenarioSnapshot:
    seed: int
    scenario: str
    pressure: float
    config: ConflictScenarioConfig
    base: ScenarioSnapshot
    cross_layer_state: CrossLayerTaskState
    proposals: tuple[LayerProposal, ...]
    ground_truth: GroundTruthResult

    @property
    def fingerprint(self) -> str:
        payload = {
            "seed": self.seed,
            "scenario": self.scenario,
            "pressure": self.pressure,
            "config": asdict(self.config),
            "base_fingerprint": self.base.fingerprint,
            "cross_layer_state": to_jsonable(self.cross_layer_state),
            "proposals": to_jsonable(self.proposals),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "scenario": self.scenario,
            "pressure": self.pressure,
            "config": asdict(self.config),
            "base_fingerprint": self.base.fingerprint,
            "scenario_fingerprint": self.fingerprint,
            "cross_layer_state": to_jsonable(self.cross_layer_state),
            "proposals": to_jsonable(self.proposals),
            "ground_truth": to_jsonable(self.ground_truth),
        }

    def instantiate(self):
        controller, verifier, provider = self.base.instantiate()
        # The business verifier supplies reachability/installation evidence;
        # cross-layer bottlenecks are evaluated by the typed scenario state.
        # Keep the synthetic probe comfortably above every scanned demand so
        # it does not introduce a second, unrelated throughput bottleneck.
        provider.base_app_rate_mbps = 100.0
        return controller, verifier, provider


class ConflictScenarioGenerator:
    """Build one immutable state/proposal/oracle snapshot per scenario+seed."""

    def __init__(self) -> None:
        self._base_generator = ElasticScenarioGenerator()
        self._ground_truth = GroundTruthSolver()

    def generate(
        self,
        scenario: str,
        pressure: float,
        seed: int,
        config: ConflictScenarioConfig | None = None,
    ) -> ConflictScenarioSnapshot:
        config = config or ConflictScenarioConfig()
        if scenario not in (*MAIN_SCENARIOS, "compound"):
            raise ValueError(f"unsupported conflict scenario: {scenario}")
        base = self._base_generator.generate(
            ScenarioConfig(
                num_agents=config.num_agents,
                edge_ratio=config.edge_ratio,
                num_gateways=config.num_gateways,
                cross_gateway_edge_ratio=config.cross_gateway_edge_ratio,
                agent_removal_ratio=0.1,
                removed_agent_type="leaf",
                multi_hop=config.multi_hop,
            ),
            seed,
        )
        edge = base.task.biz_edges[0]
        session_id = f"{base.task.task_id}-sess-1"
        gateways = tuple(
            base.gateway_paths[
                (_gateway_for(base, edge.source), _gateway_for(base, edge.target))
            ]
        )
        rng = random.Random(f"exp2:{scenario}:{pressure:.8f}:{seed}")
        jitter = rng.uniform(-0.015, 0.015)
        if scenario == "application_capacity":
            state = _application_capacity_state(
                base.task.task_id,
                edge.edge_id,
                session_id,
                gateways,
                pressure,
                jitter,
            )
        elif scenario == "transport_network":
            state = _transport_network_state(
                base.task.task_id,
                edge.edge_id,
                session_id,
                gateways,
                pressure,
                jitter,
            )
        elif scenario == "network_physical":
            state = _network_physical_state(
                base.task.task_id,
                edge.edge_id,
                session_id,
                gateways,
                pressure,
                jitter,
            )
        else:
            state = _compound_state(
                base.task.task_id,
                edge.edge_id,
                session_id,
                gateways,
                pressure,
                jitter,
            )
        proposals = collect_layer_proposals(state)
        # The oracle receives private copies.  Its immutable result contains
        # proposal identifiers and metrics, never references consumed by a
        # comparison strategy.
        ground_truth = self._ground_truth.solve(
            deepcopy(state),
            deepcopy(proposals),
        )
        return ConflictScenarioSnapshot(
            seed=seed,
            scenario=scenario,
            pressure=pressure,
            config=config,
            base=base,
            cross_layer_state=state,
            proposals=proposals,
            ground_truth=ground_truth,
        )


def _gateway_for(base: ScenarioSnapshot, agent_id: str) -> str:
    for spec in base.catalog.agents:
        if spec.agent_id == agent_id:
            return spec.gateway_id
    raise KeyError(agent_id)


def _common_state(
    task_id: str,
    edge_id: str,
    session_id: str,
    gateways: tuple[str, ...],
    scenario: str,
    pressure: float,
    *,
    application_rate: float,
    transport_rate: float,
    network_bandwidth: float,
    network_utilization: float,
    queue_occupancy: float,
    network_latency: float,
    physical_capacity: float,
    physical_reliability: float = 0.997,
    max_latency: float = 45.0,
    desired_rate: float = 10.0,
    metadata: dict[str, Any] | None = None,
) -> CrossLayerTaskState:
    route_id = f"{session_id}:route"
    access_id = f"access:{gateways[-1]}"
    base_metadata = {
        "edge_gateways": {edge_id: gateways},
        "proposal_profiles": {edge_id: {}},
        "stable_version": 1,
    }
    if metadata:
        base_metadata.update(metadata)
    return CrossLayerTaskState(
        task_id=task_id,
        scenario=scenario,
        pressure=pressure,
        application={
            edge_id: ApplicationLayerState(
                task_id=task_id,
                stage="runtime_adaptation",
                required_rate_mbps=application_rate,
                max_latency_ms=max_latency,
                max_loss_rate=0.05,
                min_reliability=0.97,
                priority=2,
                quality_level=2,
            )
        },
        transport={
            edge_id: TransportLayerState(
                session_id=session_id,
                send_rate_mbps=transport_rate,
                congestion_window=32.0,
                retransmission_rate=0.001,
                rtt_ms=5.0,
                multipath_enabled=False,
                reliability_mode="balanced",
                reliability=0.999,
            )
        },
        network={
            edge_id: NetworkLayerState(
                route_id=route_id,
                available_bandwidth_mbps=network_bandwidth,
                utilization=network_utilization,
                queue_occupancy=queue_occupancy,
                latency_ms=network_latency,
                packet_loss_rate=0.002,
                reachable=True,
                candidate_routes=("primary", "fast", "stable"),
                selected_route="primary",
                reliability=0.997,
            )
        },
        physical={
            edge_id: PhysicalLayerState(
                access_id=access_id,
                signal_quality=0.92,
                available_capacity_mbps=physical_capacity,
                resource_utilization=0.45,
                reliability=physical_reliability,
                online=True,
                access_latency_ms=2.0,
            )
        },
        constraints={
            edge_id: EdgeQoSConstraint(
                edge_id=edge_id,
                max_latency_ms=max_latency,
                max_loss_rate=0.05,
                min_reliability=0.97,
                shared_resource_id="shared-access-0",
                desired_rate_mbps=desired_rate,
            )
        },
        shared_resource_capacity_mbps={"shared-access-0": 40.0},
        metadata=base_metadata,
    )


def _application_capacity_state(
    task_id: str,
    edge_id: str,
    session_id: str,
    gateways: tuple[str, ...],
    pressure: float,
    jitter: float,
) -> CrossLayerTaskState:
    requested = 20.0 * pressure * (1.0 + jitter)
    transport_high = max(10.0, requested * 1.05)
    robust_capacity = 22.0 if pressure <= 1.0 else max(17.0, 22.0 - 10.0 * (pressure - 1.0))
    state = _common_state(
        task_id,
        edge_id,
        session_id,
        gateways,
        "application_capacity",
        pressure,
        application_rate=10.0,
        transport_rate=10.0,
        network_bandwidth=20.0,
        network_utilization=0.30,
        queue_occupancy=0.12,
        network_latency=8.0,
        physical_capacity=22.0,
        desired_rate=requested,
        metadata={
            "shared_resource_background_demand_mbps": {
                "shared-access-0": 8.0,
            }
        },
    )
    state = replace(
        state,
        shared_resource_capacity_mbps={"shared-access-0": 37.0},
    )
    profiles = {
        "application": (
            _profile("KEEP_QUALITY", 0.0, application_rate_mbps=10.0, quality_level=2),
            _profile("INCREASE_APPLICATION_RATE", 3.2, 0.12, application_rate_mbps=requested, quality_level=3),
            _profile("DECREASE_APPLICATION_RATE", 0.2, 0.25, application_rate_mbps=10.0, quality_level=2),
        ),
        "transport": (
            _profile("KEEP_TRANSPORT", 0.0, transport_rate_mbps=10.0),
            _profile("INCREASE_SEND_RATE", 2.8, 0.10, transport_rate_mbps=transport_high, congestion_window=48.0),
            _profile("DECREASE_SEND_RATE", 0.3, 0.15, transport_rate_mbps=max(10.0, requested), congestion_window=28.0),
        ),
        "network": (
            _profile("KEEP_ROUTE", 0.0, network_bandwidth_mbps=20.0),
            _profile("RESERVE_BANDWIDTH", 1.8, 0.20, network_bandwidth_mbps=max(20.0, transport_high), network_utilization=0.34),
            _profile("SWITCH_ROUTE", 0.9, 0.45, route_id=f"{session_id}:route-safe", selected_route="stable", network_bandwidth_mbps=34.0, network_latency_ms=12.0, network_utilization=0.28),
        ),
        "physical": (
            _profile("KEEP_RESOURCE", 0.0, physical_capacity_mbps=22.0),
            _profile("REALLOCATE_RESOURCE", 1.7, 0.12, physical_capacity_mbps=robust_capacity, physical_reliability=0.999, signal_quality=0.96),
            _profile("ALLOCATE_RESOURCE", 0.9, 0.55, physical_capacity_mbps=36.0, physical_reliability=0.997, physical_utilization=0.72),
        ),
    }
    return _with_profiles(state, edge_id, profiles)


def _transport_network_state(
    task_id: str,
    edge_id: str,
    session_id: str,
    gateways: tuple[str, ...],
    pressure: float,
    jitter: float,
) -> CrossLayerTaskState:
    utilization = min(0.99, max(0.0, pressure + jitter))
    state = _common_state(
        task_id,
        edge_id,
        session_id,
        gateways,
        "transport_network",
        pressure,
        application_rate=10.0,
        transport_rate=10.0,
        network_bandwidth=30.0,
        network_utilization=utilization,
        queue_occupancy=0.35 * utilization,
        network_latency=8.0,
        physical_capacity=40.0,
        max_latency=45.0,
    )
    profiles = {
        "application": (
            _profile("KEEP_QUALITY", 0.0, application_rate_mbps=10.0),
            _profile("INCREASE_APPLICATION_RATE", 1.4, 0.10, application_rate_mbps=15.0, quality_level=3),
            _profile("DECREASE_APPLICATION_RATE", 0.2, 0.20, application_rate_mbps=10.0),
        ),
        "transport": (
            _profile("KEEP_TRANSPORT", 0.0, transport_rate_mbps=10.0),
            _profile("INCREASE_SEND_RATE", 3.0, 0.12, transport_rate_mbps=18.0, congestion_window=64.0, transport_rtt_ms=6.0),
            _profile("DECREASE_SEND_RATE", 0.5, 0.15, transport_rate_mbps=15.0, congestion_window=24.0, transport_rtt_ms=5.0),
        ),
        "network": (
            _profile("KEEP_ROUTE", 0.0, network_bandwidth_mbps=30.0, network_utilization=utilization, queue_occupancy=0.35 * utilization),
            _profile("RESERVE_BANDWIDTH", 2.0, 0.15, network_bandwidth_mbps=32.0, network_utilization=utilization, queue_occupancy=0.35 * utilization),
            _profile("SWITCH_ROUTE", 1.25, 0.38, route_id=f"{session_id}:route-relief", selected_route="stable", network_bandwidth_mbps=28.0, network_utilization=0.35, queue_occupancy=0.12, network_latency_ms=11.0),
        ),
        "physical": (
            _profile("KEEP_RESOURCE", 0.0, physical_capacity_mbps=40.0),
            _profile("ALLOCATE_RESOURCE", 0.45, 0.12, physical_capacity_mbps=42.0, physical_utilization=0.55),
            _profile("REALLOCATE_RESOURCE", 0.2, 0.20, physical_capacity_mbps=38.0, physical_reliability=0.999),
        ),
    }
    return _with_profiles(state, edge_id, profiles)


def _network_physical_state(
    task_id: str,
    edge_id: str,
    session_id: str,
    gateways: tuple[str, ...],
    pressure: float,
    jitter: float,
) -> CrossLayerTaskState:
    drop = min(0.9, max(0.0, pressure + jitter))
    primary_access = f"access:{gateways[-1]}"
    state = _common_state(
        task_id,
        edge_id,
        session_id,
        gateways,
        "network_physical",
        pressure,
        application_rate=10.0,
        transport_rate=10.0,
        network_bandwidth=25.0,
        network_utilization=0.35,
        queue_occupancy=0.10,
        network_latency=12.0,
        physical_capacity=25.0,
        physical_reliability=0.997,
        metadata={
            "route_access_requirements": {
                edge_id: {
                    "primary": primary_access,
                    "fast": "access-fast",
                    "stable": "access-stable",
                }
            }
        },
    )
    fast_capacity = 25.0 * (1.0 - drop)
    profiles = {
        "application": (
            _profile("KEEP_QUALITY", 0.0, application_rate_mbps=10.0),
            _profile("INCREASE_QUALITY", 1.0, 0.12, application_rate_mbps=12.0, quality_level=3),
            _profile("DECREASE_QUALITY", 0.1, 0.20, application_rate_mbps=10.0, quality_level=2),
        ),
        "transport": (
            _profile("KEEP_TRANSPORT", 0.0, transport_rate_mbps=10.0),
            _profile("INCREASE_SEND_RATE", 1.1, 0.10, transport_rate_mbps=12.0, congestion_window=40.0),
            _profile("ENABLE_MULTIPATH", 0.5, 0.24, transport_rate_mbps=12.0, multipath_enabled=True),
        ),
        "network": (
            _profile("KEEP_ROUTE", 0.0, selected_route="primary", route_id=f"{session_id}:route", network_bandwidth_mbps=25.0, network_latency_ms=12.0),
            _profile("SWITCH_ROUTE", 2.8, 0.15, selected_route="fast", route_id=f"{session_id}:route-fast", network_bandwidth_mbps=20.0, network_latency_ms=6.0, network_utilization=0.30),
            _profile("SWITCH_ROUTE", 1.45, 0.30, selected_route="stable", route_id=f"{session_id}:route-stable", network_bandwidth_mbps=18.0, network_latency_ms=10.0, network_utilization=0.28),
        ),
        "physical": (
            _profile("KEEP_RESOURCE", 0.0, access_id=primary_access, physical_capacity_mbps=25.0),
            _profile("REALLOCATE_RESOURCE", 2.4, 0.15, access_id="access-fast", physical_capacity_mbps=fast_capacity, physical_reliability=max(0.97, 0.997 - 0.02 * drop), signal_quality=max(0.35, 0.94 - 0.55 * drop)),
            _profile("SWITCH_ACCESS", 1.35, 0.32, access_id="access-stable", physical_capacity_mbps=24.0, physical_reliability=0.998, signal_quality=0.91, access_latency_ms=3.0),
        ),
    }
    return _with_profiles(state, edge_id, profiles)


def _compound_state(
    task_id: str,
    edge_id: str,
    session_id: str,
    gateways: tuple[str, ...],
    pressure: float,
    jitter: float,
) -> CrossLayerTaskState:
    # pressure is a normalized time-series level in [0, 1].  Combining the
    # three mechanisms yields the requested end-to-end case without changing
    # the authorized DAG.
    rate_pressure = 0.7 + 0.7 * pressure
    state = _application_capacity_state(
        task_id,
        edge_id,
        session_id,
        gateways,
        rate_pressure,
        jitter,
    )
    network = state.network[edge_id]
    physical = state.physical[edge_id]
    return CrossLayerTaskState(
        task_id=state.task_id,
        scenario="compound",
        pressure=pressure,
        application=state.application,
        transport=state.transport,
        network={
            edge_id: NetworkLayerState(
                **{
                    **asdict(network),
                    "utilization": min(0.95, 0.35 + 0.55 * pressure),
                    "queue_occupancy": 0.12 + 0.30 * pressure,
                }
            )
        },
        physical={
            edge_id: PhysicalLayerState(
                **{
                    **asdict(physical),
                    "available_capacity_mbps": max(18.0, 30.0 - 12.0 * pressure),
                    "signal_quality": max(0.45, 0.95 - 0.45 * pressure),
                }
            )
        },
        constraints=state.constraints,
        shared_resource_capacity_mbps=state.shared_resource_capacity_mbps,
        metadata=state.metadata,
    )


def _profile(
    action: str,
    expected_qos_gain: float,
    expected_cost: float = 0.0,
    **parameters: object,
) -> dict[str, object]:
    return {
        "action": action,
        "expected_qos_gain": expected_qos_gain,
        "expected_cost": expected_cost,
        "confidence": 0.95,
        "parameters": parameters,
    }


def _with_profiles(
    state: CrossLayerTaskState,
    edge_id: str,
    profiles: dict[str, tuple[dict[str, object], ...]],
) -> CrossLayerTaskState:
    metadata = dict(state.metadata)
    proposal_profiles = dict(metadata.get("proposal_profiles", {}))
    proposal_profiles[edge_id] = profiles
    metadata["proposal_profiles"] = proposal_profiles
    return CrossLayerTaskState(
        task_id=state.task_id,
        scenario=state.scenario,
        pressure=state.pressure,
        application=state.application,
        transport=state.transport,
        network=state.network,
        physical=state.physical,
        constraints=state.constraints,
        shared_resource_capacity_mbps=state.shared_resource_capacity_mbps,
        applied_proposal_ids=state.applied_proposal_ids,
        metadata=metadata,
    )
