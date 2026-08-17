from __future__ import annotations

import hashlib
import json
import random
from copy import deepcopy
from dataclasses import replace
from typing import Any

from src.agents.layer_proposals import collect_layer_proposals
from src.core.cross_layer import (
    ApplicationLayerState,
    CrossLayerTaskState,
    EdgeQoSConstraint,
    NetworkLayerState,
    PhysicalLayerState,
    TransportLayerState,
)
from src.simulation.demand_capacity_truth import solve_truth
from src.simulation.conflict_robustness import RobustnessScenarioSnapshot
from src.simulation.conflict_scenario_generator import (
    ConflictScenarioConfig,
    ConflictScenarioGenerator,
)


LAYERS = ("transport", "network", "physical")
SHARED_RESOURCE_ID = "gamma-shared-resource"


class DemandCapacityRatioGenerator:
    """Build a paired three-flow snapshot with gamma as the only control.

    One deterministic 10-agent task is generated per seed. Three of its
    cross-gateway business flows share one capacity budget. Each flow gets an
    independently sampled transport, network, and physical capacity, while a
    seed-permuted layer label identifies which of those three is its initial
    bottleneck. Candidate resources are also sampled once per seed. Therefore
    changing gamma changes requested rates and their direct derivatives only.
    """

    distribution_version = "wcnc-final-gamma-v5"
    environment_rng_version = "wcnc-final-gamma-environment-v4"
    _environment_seed_namespace = "wcnc-final-gamma-v4"

    def __init__(self) -> None:
        self._base = ConflictScenarioGenerator()

    def generate(
        self,
        gamma: float,
        seed: int,
        config: ConflictScenarioConfig | None = None,
    ) -> RobustnessScenarioSnapshot:
        if gamma <= 0.0:
            raise ValueError("demand-to-capacity ratio must be positive")
        config = config or ConflictScenarioConfig()
        if config.num_agents != 10:
            raise ValueError("demand/capacity v2 requires one 10-agent task")

        # The base pressure is deliberately constant: it is nuisance state,
        # not the controlled demand-to-capacity ratio.
        base = self._base.generate("application_capacity", 0.6, seed, config)
        topology = base.base
        gateway_by_agent = {
            spec.agent_id: spec.gateway_id for spec in topology.catalog.agents
        }
        cross_gateway_edges = tuple(
            edge
            for edge in sorted(
                topology.task.biz_edges,
                key=lambda item: (-item.priority, item.edge_id),
            )
            if gateway_by_agent[edge.source] != gateway_by_agent[edge.target]
        )
        if len(cross_gateway_edges) < 3:
            raise ValueError("task snapshot has fewer than three cross-gateway flows")
        selected_edges = cross_gateway_edges[:3]

        rng = random.Random(f"{self._environment_seed_namespace}:{seed}")
        shared_trunk_utilization = rng.uniform(0.75, 0.90)
        bottleneck_layers = list(LAYERS)
        rng.shuffle(bottleneck_layers)

        flow_environment: dict[str, dict[str, Any]] = {}
        candidate_resources: dict[str, dict[str, Any]] = {}
        edge_gateways: dict[str, tuple[str, ...]] = {}
        for edge, bottleneck in zip(selected_edges, bottleneck_layers):
            source_gateway = gateway_by_agent[edge.source]
            target_gateway = gateway_by_agent[edge.target]
            gateways = tuple(
                topology.gateway_paths[(source_gateway, target_gateway)]
            )
            edge_gateways[edge.edge_id] = gateways

            # Each layer is sampled separately. The label controls its sampling
            # stratum; no layer value or recovery headroom is derived from any
            # other layer's value.
            capacities = {
                layer: rng.uniform(9.0, 11.0)
                if layer == bottleneck
                else rng.uniform(12.0, 14.0)
                for layer in LAYERS
            }
            recoveries = {layer: rng.uniform(15.5, 17.0) for layer in LAYERS}
            adaptation_factor = rng.uniform(0.80, 0.84)
            primary_access = f"access:{gateways[-1]}"
            alternate_access = f"{edge.edge_id}:alternate-access"
            requires_access_switch = rng.random() < 0.55
            flow_environment[edge.edge_id] = {
                "source": edge.source,
                "target": edge.target,
                "gateway_path": gateways,
                "bottleneck_layer": bottleneck,
                "transport_admissible_mbps": capacities["transport"],
                "network_available_mbps": capacities["network"],
                "physical_available_mbps": capacities["physical"],
                "effective_mbps": min(capacities.values()),
                "transport_recovery_mbps": recoveries["transport"],
                "network_recovery_mbps": recoveries["network"],
                "physical_recovery_mbps": recoveries["physical"],
                "application_adaptation_factor": adaptation_factor,
                "network_delay_ms": rng.uniform(6.0, 9.0),
                "network_utilization": rng.uniform(0.12, 0.24),
                "queue_occupancy": rng.uniform(0.04, 0.10),
                "packet_loss_rate": rng.uniform(0.0005, 0.0015),
                "physical_signal_quality": rng.uniform(0.92, 0.98),
                "physical_resource_utilization": rng.uniform(0.18, 0.36),
                "access_latency_ms": rng.uniform(1.5, 2.5),
            }
            candidate_resources[edge.edge_id] = {
                "reserved_route": f"{edge.edge_id}:reserved-route",
                "primary_access": primary_access,
                "alternate_access": alternate_access,
                "reserved_access": (
                    alternate_access if requires_access_switch else primary_access
                ),
                "requires_access_switch": requires_access_switch,
                "transport_recovery_mbps": recoveries["transport"],
                "network_recovery_mbps": recoveries["network"],
                "physical_recovery_mbps": recoveries["physical"],
            }

        environment = {
            "distribution_version": self.distribution_version,
            "environment_rng_version": self.environment_rng_version,
            "seed": seed,
            "task_fingerprint": topology.fingerprint,
            "selected_flow_ids": tuple(edge.edge_id for edge in selected_edges),
            "critical_flow_selection_criterion": (
                "highest_task_priority_then_canonical_edge_id"
            ),
            "flow_priorities": {
                edge.edge_id: edge.priority for edge in selected_edges
            },
            "flow_environment": flow_environment,
            "candidate_resources": candidate_resources,
            "shared_trunk_utilization": shared_trunk_utilization,
        }
        environment_fingerprint = hashlib.sha256(
            json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        application: dict[str, ApplicationLayerState] = {}
        transport: dict[str, TransportLayerState] = {}
        network: dict[str, NetworkLayerState] = {}
        physical: dict[str, PhysicalLayerState] = {}
        constraints: dict[str, EdgeQoSConstraint] = {}
        proposal_profiles: dict[str, dict[str, tuple[dict[str, object], ...]]] = {}
        flow_capacities: dict[str, dict[str, float]] = {}
        flow_bottlenecks: dict[str, str] = {}
        flow_r_req: dict[str, float] = {}

        for edge in selected_edges:
            edge_id = edge.edge_id
            values = flow_environment[edge_id]
            c_t = float(values["transport_admissible_mbps"])
            c_n = float(values["network_available_mbps"])
            c_p = float(values["physical_available_mbps"])
            c_eff = float(values["effective_mbps"])
            r_req = float(gamma) * c_eff
            adapted_rate = r_req * float(values["application_adaptation_factor"])
            transport_recovery = float(values["transport_recovery_mbps"])
            network_recovery = float(values["network_recovery_mbps"])
            physical_recovery = float(values["physical_recovery_mbps"])
            quality_loss = 1.0 - adapted_rate / max(r_req, 1e-9)
            transport_increment = max(0.0, transport_recovery - c_t)
            network_increment = max(0.0, network_recovery - c_n)
            physical_increment = max(0.0, physical_recovery - c_p)
            transport_increment_fraction = transport_increment / max(c_t, 1e-9)
            network_increment_fraction = network_increment / max(c_n, 1e-9)
            physical_increment_fraction = physical_increment / max(c_p, 1e-9)
            transport_gain = _normalized_deficit_reduction(
                r_req, c_t, transport_recovery
            )
            network_gain = _normalized_deficit_reduction(
                r_req, c_n, network_recovery
            )
            physical_gain = _normalized_deficit_reduction(
                r_req, c_p, physical_recovery
            )
            application_cost = quality_loss + 0.02
            transport_cost = 0.15 * transport_increment_fraction + 0.02
            network_cost = 0.15 * network_increment_fraction + 0.02
            physical_cost = 0.15 * physical_increment_fraction + 0.02
            flow_r_req[edge_id] = r_req
            flow_capacities[edge_id] = {
                "transport_admissible_mbps": c_t,
                "network_available_mbps": c_n,
                "physical_available_mbps": c_p,
                "effective_mbps": c_eff,
            }
            flow_bottlenecks[edge_id] = str(values["bottleneck_layer"])

            session_id = f"{topology.task.task_id}:{edge_id}:session"
            route_id = f"{edge_id}:primary-route"
            access_id = str(candidate_resources[edge_id]["primary_access"])
            application[edge_id] = ApplicationLayerState(
                task_id=topology.task.task_id,
                stage="runtime_adaptation",
                required_rate_mbps=r_req,
                max_latency_ms=35.0,
                max_loss_rate=0.05,
                min_reliability=0.98,
                priority=2,
                quality_level=2,
            )
            transport[edge_id] = TransportLayerState(
                session_id=session_id,
                send_rate_mbps=r_req,
                congestion_window=32.0,
                retransmission_rate=0.0005,
                rtt_ms=4.0,
                multipath_enabled=False,
                reliability_mode="balanced",
                reliability=0.9995,
                admissible_capacity_mbps=c_t,
            )
            network[edge_id] = NetworkLayerState(
                route_id=route_id,
                available_bandwidth_mbps=c_n,
                utilization=float(values["network_utilization"]),
                queue_occupancy=float(values["queue_occupancy"]),
                latency_ms=float(values["network_delay_ms"]),
                packet_loss_rate=float(values["packet_loss_rate"]),
                reachable=True,
                candidate_routes=(
                    "primary",
                    str(candidate_resources[edge_id]["reserved_route"]),
                ),
                selected_route="primary",
                reliability=0.9995,
            )
            physical[edge_id] = PhysicalLayerState(
                access_id=access_id,
                signal_quality=float(values["physical_signal_quality"]),
                available_capacity_mbps=c_p,
                resource_utilization=float(values["physical_resource_utilization"]),
                reliability=0.9995,
                online=True,
                access_latency_ms=float(values["access_latency_ms"]),
            )
            constraints[edge_id] = EdgeQoSConstraint(
                edge_id=edge_id,
                max_latency_ms=35.0,
                max_loss_rate=0.05,
                min_reliability=0.98,
                shared_resource_id=SHARED_RESOURCE_ID,
                desired_rate_mbps=adapted_rate,
            )
            proposal_profiles[edge_id] = {
                "application": (
                    _profile(
                        "KEEP_QUALITY",
                        2.0,
                        application_rate_mbps=r_req,
                        quality_level=2,
                    ),
                    _profile(
                        "DECREASE_APPLICATION_RATE",
                        max(0.0, (r_req - adapted_rate) / max(r_req, 1e-9)),
                        application_cost,
                        application_rate_mbps=adapted_rate,
                        quality_level=1,
                        quality_loss_fraction=quality_loss,
                        normalized_action_cost=application_cost,
                    ),
                ),
                "transport": (
                    _profile(
                        "KEEP_TRANSPORT",
                        0.0,
                        transport_rate_mbps=r_req,
                        transport_admissible_capacity_mbps=c_t,
                    ),
                    _profile(
                        "RESERVE_TRANSPORT_SERVICE",
                        transport_gain,
                        transport_cost,
                        transport_rate_mbps=r_req,
                        transport_admissible_capacity_mbps=transport_recovery,
                        multipath_enabled=True,
                        reserved_increment_mbps=transport_increment,
                        reserved_increment_fraction=transport_increment_fraction,
                        normalized_action_cost=transport_cost,
                    ),
                ),
                "network": (
                    _profile(
                        "KEEP_ROUTE",
                        0.0,
                        selected_route="primary",
                        network_bandwidth_mbps=c_n,
                    ),
                    _profile(
                        "RESERVE_BANDWIDTH",
                        network_gain,
                        network_cost,
                        route_id=str(candidate_resources[edge_id]["reserved_route"]),
                        selected_route=str(
                            candidate_resources[edge_id]["reserved_route"]
                        ),
                        network_bandwidth_mbps=network_recovery,
                        reserved_increment_mbps=network_increment,
                        reserved_increment_fraction=network_increment_fraction,
                        normalized_action_cost=network_cost,
                    ),
                ),
                "physical": (
                    _profile(
                        "KEEP_RESOURCE",
                        0.0,
                        access_id=access_id,
                        physical_capacity_mbps=c_p,
                    ),
                    _profile(
                        "REALLOCATE_RESOURCE",
                        physical_gain,
                        physical_cost,
                        access_id=str(
                            candidate_resources[edge_id]["alternate_access"]
                        ),
                        physical_capacity_mbps=physical_recovery,
                        signal_quality=0.97,
                        reserved_increment_mbps=physical_increment,
                        reserved_increment_fraction=physical_increment_fraction,
                        normalized_action_cost=physical_cost,
                    ),
                ),
            }

        metadata = {
            "stable_version": 2,
            "demand_capacity_definition": (
                "gamma = R_req / min(C_t_admissible, C_n_available, C_p_available)"
            ),
            "demand_to_capacity_ratio": float(gamma),
            "flow_r_req_mbps": flow_r_req,
            "flow_capacities": flow_capacities,
            "flow_bottlenecks": flow_bottlenecks,
            "candidate_resources": candidate_resources,
            "environment_fingerprint": environment_fingerprint,
            "random_environment": environment,
            "shared_trunk_utilization": shared_trunk_utilization,
            "edge_gateways": edge_gateways,
            "proposal_profiles": proposal_profiles,
            "ground_truth_conflict_reference": "keep_combination",
            "common_objective_version": "normalized-deficit-cost-v1",
            "independent_layer_capacities": True,
            "shared_demand_uses_application_rate": True,
            "shared_resource_background_demand_mbps": {
                SHARED_RESOURCE_ID: 0.0
            },
            "route_access_requirements": {
                edge_id: {
                    str(resources["reserved_route"]): str(
                        resources["reserved_access"]
                    )
                }
                for edge_id, resources in candidate_resources.items()
            },
        }
        shared_trunk_capacity = sum(
            values["effective_mbps"] for values in flow_capacities.values()
        ) / shared_trunk_utilization
        state = CrossLayerTaskState(
            task_id=topology.task.task_id,
            scenario="demand_capacity_ratio_v5",
            pressure=float(gamma),
            application=application,
            transport=transport,
            network=network,
            physical=physical,
            constraints=constraints,
            shared_resource_capacity_mbps={
                SHARED_RESOURCE_ID: shared_trunk_capacity
            },
            metadata=metadata,
        )
        proposals = collect_layer_proposals(state)
        ground_truth = solve_truth(deepcopy(state), deepcopy(proposals))
        return RobustnessScenarioSnapshot(
            seed=seed,
            experiment="demand_capacity_ratio_v5",
            scenario="demand_capacity_ratio_v5",
            pressure=float(gamma),
            base=base,
            true_state=state,
            observed_state=state,
            proposals=proposals,
            ground_truth=ground_truth,
            metadata={
                "distribution_version": self.distribution_version,
                "environment_rng_version": self.environment_rng_version,
                "truth_evaluator": "independent-demand-capacity-truth-v1",
                "num_flows": 3,
                "proposals_per_layer": 2,
                "candidate_combinations": ground_truth.candidate_combinations,
                "observation_source": "stable_true_state",
                "ground_truth_state": "true_state",
                "demand_to_capacity_ratio": float(gamma),
                "flow_r_req_mbps": flow_r_req,
                "flow_capacities": flow_capacities,
                "flow_bottlenecks": flow_bottlenecks,
                "candidate_resources": candidate_resources,
                "shared_trunk_utilization": shared_trunk_utilization,
                "shared_trunk_capacity_mbps": shared_trunk_capacity,
                "critical_flow_selection_criterion": (
                    "highest_task_priority_then_canonical_edge_id"
                ),
                "flow_priorities": {
                    edge.edge_id: edge.priority for edge in selected_edges
                },
                "environment_fingerprint": environment_fingerprint,
                "random_environment": environment,
            },
        )


def _profile(
    action: str,
    gain: float,
    cost: float = 0.0,
    **parameters: object,
) -> dict[str, object]:
    return {
        "action": action,
        "expected_qos_gain": gain,
        "expected_cost": cost,
        "confidence": 0.95,
        "parameters": parameters,
    }


def _normalized_deficit_reduction(
    required_mbps: float,
    current_mbps: float,
    candidate_mbps: float,
) -> float:
    scale = max(required_mbps, 1e-9)
    before = max(0.0, required_mbps - current_mbps) / scale
    after = max(0.0, required_mbps - candidate_mbps) / scale
    return max(0.0, before - after)
