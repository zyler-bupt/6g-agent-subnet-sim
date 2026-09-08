from __future__ import annotations

import random
from copy import deepcopy
from dataclasses import replace

from src.agents.layer_proposals import collect_layer_proposals
from src.controller.ground_truth import GroundTruthSolver
from src.core.cross_layer import (
    ApplicationLayerState,
    CrossLayerTaskState,
    EdgeQoSConstraint,
    NetworkLayerState,
    PhysicalLayerState,
    TransportLayerState,
)
from src.simulation.conflict_robustness import RobustnessScenarioSnapshot
from src.simulation.conflict_scenario_generator import (
    ConflictScenarioConfig,
    ConflictScenarioGenerator,
)


class CrossLayerStressGenerator:
    """Generate a continuous feasibility transition from one frozen model.

    The class label is never sampled or injected.  Demand, residual network
    bandwidth, queue state, physical capacity and QoS bounds are drawn first;
    an independent exhaustive oracle classifies the resulting proposal space.
    """

    def __init__(self) -> None:
        self._base = ConflictScenarioGenerator()
        self._oracle = GroundTruthSolver()

    def generate(
        self,
        intensity: float,
        seed: int,
        config: ConflictScenarioConfig | None = None,
    ) -> RobustnessScenarioSnapshot:
        if intensity <= 0.0:
            raise ValueError("conflict intensity must be positive")
        base = self._base.generate("application_capacity", 0.6, seed, config)
        source = base.cross_layer_state
        edge_id = next(iter(source.constraints))
        application = source.application[edge_id]
        transport = source.transport[edge_id]
        network = source.network[edge_id]
        physical = source.physical[edge_id]
        rng = random.Random(f"wcnc-cross-layer-stress:{seed}")

        nominal = rng.uniform(9.0, 11.0)
        stress = _clip((intensity - 0.6) / 0.9, 0.0, 1.0)
        demand = nominal * (0.90 + 0.30 * stress) * rng.uniform(0.97, 1.03)
        minimum_service_rate = demand * rng.uniform(0.76, 0.80)
        network_capacity = nominal * (
            1.75 - 0.80 * stress + rng.uniform(-0.10, 0.10)
        )
        physical_capacity = nominal * (
            1.90 - 0.95 * stress + rng.uniform(-0.11, 0.11)
        )
        utilization = _clip(
            0.25 + 0.50 * stress + rng.uniform(-0.035, 0.035),
            0.05,
            0.97,
        )
        queue = _clip(
            0.08 + 0.30 * stress + rng.uniform(-0.025, 0.025),
            0.01,
            0.80,
        )
        max_latency = max(
            22.0,
            40.0 - 9.0 * stress + rng.uniform(-2.2, 2.2),
        )
        min_reliability = _clip(
            0.975 + 0.016 * stress + rng.uniform(-0.002, 0.002),
            0.95,
            0.997,
        )
        shared_capacity = nominal * (
            2.20 - 0.45 * stress + rng.uniform(-0.12, 0.12)
        )
        background_demand = nominal * (
            0.18 + 0.35 * stress + rng.uniform(-0.04, 0.04)
        )

        network_stable = nominal * (
            1.70 - 0.60 * stress + rng.uniform(-0.15, 0.15)
        )
        physical_stable = nominal * (
            1.75 - 0.75 * stress + rng.uniform(-0.16, 0.16)
        )
        physical_stable_reliability = _clip(
            0.999
            - 0.010 * stress
            + rng.uniform(-0.003, 0.001),
            0.96,
            0.9995,
        )
        base_metadata = dict(source.metadata)
        base_metadata.update(
            {
                "stable_version": 1,
                "stress_latent_values": {
                    "nominal_rate_mbps": nominal,
                    "background_demand_mbps": background_demand,
                },
                "shared_resource_background_demand_mbps": {
                    "stress-shared-resource": max(0.0, background_demand),
                },
            }
        )
        state = CrossLayerTaskState(
            task_id=source.task_id,
            scenario="cross_layer_stress",
            pressure=float(intensity),
            application={
                edge_id: replace(
                    application,
                    required_rate_mbps=demand,
                    max_latency_ms=max_latency,
                    max_loss_rate=0.05,
                    min_reliability=min_reliability,
                )
            },
            transport={
                edge_id: replace(
                    transport,
                    send_rate_mbps=demand * 1.02,
                    congestion_window=32.0,
                    retransmission_rate=0.0015,
                    rtt_ms=5.0,
                    reliability=0.999,
                )
            },
            network={
                edge_id: replace(
                    network,
                    available_bandwidth_mbps=max(0.1, network_capacity),
                    utilization=utilization,
                    queue_occupancy=queue,
                    latency_ms=8.0 + 4.0 * stress + rng.uniform(-0.8, 0.8),
                    packet_loss_rate=_clip(0.001 + 0.004 * stress, 0.0, 0.03),
                    reliability=0.998,
                    candidate_routes=("primary", "reserved", "stable"),
                    selected_route="primary",
                )
            },
            physical={
                edge_id: replace(
                    physical,
                    signal_quality=_clip(0.98 - 0.25 * stress, 0.45, 0.99),
                    available_capacity_mbps=max(0.1, physical_capacity),
                    resource_utilization=_clip(0.20 + 0.50 * stress, 0.0, 0.95),
                    reliability=_clip(0.999 - 0.009 * stress, 0.97, 0.999),
                    access_latency_ms=2.0 + 1.2 * stress,
                )
            },
            constraints={
                edge_id: EdgeQoSConstraint(
                    edge_id=edge_id,
                    max_latency_ms=max_latency,
                    max_loss_rate=0.05,
                    min_reliability=min_reliability,
                    shared_resource_id="stress-shared-resource",
                    desired_rate_mbps=minimum_service_rate,
                )
            },
            shared_resource_capacity_mbps={
                "stress-shared-resource": max(0.1, shared_capacity)
            },
            metadata=base_metadata,
        )
        profiles = {
            "application": (
                _profile("KEEP_QUALITY", 0.0, application_rate_mbps=demand, quality_level=2),
                _profile("INCREASE_APPLICATION_RATE", 2.50, 0.10, application_rate_mbps=demand * 1.12, quality_level=3),
                _profile("DECREASE_APPLICATION_RATE", 0.50, 0.35, application_rate_mbps=demand * 0.80, quality_level=1),
            ),
            "transport": (
                _profile("KEEP_TRANSPORT", 0.0, transport_rate_mbps=demand * 1.02),
                _profile("INCREASE_SEND_RATE", 2.30, 0.10, transport_rate_mbps=demand * 1.18, congestion_window=64.0),
                _profile("DECREASE_SEND_RATE", 0.80, 0.30, transport_rate_mbps=demand * 0.82, congestion_window=24.0),
            ),
            "network": (
                _profile("KEEP_ROUTE", 0.0, selected_route="primary", network_bandwidth_mbps=max(0.1, network_capacity), network_utilization=utilization, queue_occupancy=queue),
                _profile("RESERVE_BANDWIDTH", 2.00, 0.15, selected_route="reserved", network_bandwidth_mbps=max(0.1, network_capacity * 1.08), network_utilization=min(0.98, utilization + 0.03), queue_occupancy=min(0.9, queue + 0.03)),
                _profile("SWITCH_ROUTE", 1.20, 0.32, selected_route="stable", route_id=f"{network.route_id}:stable", network_bandwidth_mbps=max(0.1, network_stable), network_utilization=_clip(0.22 + 0.28 * stress, 0.05, 0.75), queue_occupancy=_clip(0.12 + 0.12 * stress, 0.02, 0.50), network_latency_ms=8.0 + 6.0 * stress),
            ),
            "physical": (
                _profile("KEEP_RESOURCE", 0.0, access_id=physical.access_id, physical_capacity_mbps=max(0.1, physical_capacity), physical_reliability=state.physical[edge_id].reliability),
                _profile("REALLOCATE_RESOURCE", 1.80, 0.16, access_id=physical.access_id, physical_capacity_mbps=max(0.1, physical_capacity * 1.08), physical_reliability=state.physical[edge_id].reliability, signal_quality=state.physical[edge_id].signal_quality),
                _profile("SWITCH_ACCESS", 1.10, 0.34, access_id="stress-stable-access", physical_capacity_mbps=max(0.1, physical_stable), physical_reliability=physical_stable_reliability, signal_quality=0.91, access_latency_ms=3.0),
            ),
        }
        metadata = dict(state.metadata)
        metadata["proposal_profiles"] = {edge_id: profiles}
        metadata["stress_definition"] = {
            "normalized_stress": "clip((rho-0.6)/0.9,0,1)",
            "application_demand": "nominal*(0.90+0.30*stress)*seed_jitter",
            "transport_aggressive_rate": "1.18*application_demand",
            "network_residual": "nominal*(1.75-0.80*stress+seed_jitter)",
            "network_utilization": "clip(0.25+0.50*stress+seed_jitter)",
            "physical_residual": "nominal*(1.90-0.95*stress+seed_jitter)",
            "maximum_latency": "40-9*stress+seed_jitter",
        }
        state = replace(state, metadata=metadata)
        proposals = collect_layer_proposals(state)
        ground_truth = self._oracle.solve(deepcopy(state), deepcopy(proposals))
        return RobustnessScenarioSnapshot(
            seed=seed,
            experiment="cross_layer_stress",
            scenario="cross_layer_stress",
            pressure=float(intensity),
            base=base,
            true_state=state,
            observed_state=state,
            proposals=proposals,
            ground_truth=ground_truth,
            metadata={
                "proposals_per_layer": 3,
                "observation_source": "stable_true_state",
                "ground_truth_state": "true_state",
                "intensity_definition": metadata["stress_definition"],
            },
        )


def _profile(action: str, gain: float, cost: float = 0.0, **parameters: object) -> dict[str, object]:
    return {
        "action": action,
        "expected_qos_gain": gain,
        "expected_cost": cost,
        "confidence": 0.95,
        "parameters": parameters,
    }


def _clip(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))
