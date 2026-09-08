from __future__ import annotations

import hashlib
import json
import random
from copy import deepcopy
from dataclasses import replace

from src.agents.layer_proposals import collect_layer_proposals
from src.simulation.conflict_robustness import RobustnessScenarioSnapshot
from src.simulation.conflict_scenario_generator import ConflictScenarioConfig
from src.simulation.demand_capacity_ratio import DemandCapacityRatioGenerator
from src.simulation.demand_capacity_truth import solve_truth


class HeterogeneousDemandCapacityGenerator:
    """Canonical v3 demand generator, isolated from hash-frozen legacy v5."""

    distribution_version = "wcnc-final-v3-heterogeneous-demand-v1"
    observed_state_schema_version = "exp2-observed-v1"
    true_state_schema_version = "exp2-true-v1"

    def __init__(self) -> None:
        self._legacy = DemandCapacityRatioGenerator()

    def generate(
        self, gamma: float, seed: int,
        config: ConflictScenarioConfig | None = None,
    ) -> RobustnessScenarioSnapshot:
        legacy = self._legacy.generate(gamma, seed, config)
        flow_ids = tuple(sorted(legacy.true_state.application))
        rng = random.Random(f"wcnc-final-v3-epsilon:{seed}")
        raw = [rng.uniform(-0.15, 0.15) for _ in flow_ids]
        centered = [value - sum(raw) / len(raw) for value in raw]
        scale = max(1.0, max(abs(value) for value in centered) / 0.15)
        epsilon = {flow_id: 1.0 + centered[index] / scale for index, flow_id in enumerate(flow_ids)}

        metadata = deepcopy(legacy.true_state.metadata)
        metadata["demand_capacity_definition"] = "R_i = gamma * C_eff_i * epsilon_i"
        metadata["epsilon_by_flow"] = epsilon
        flow_rates = {}
        applications = {}; transports = {}; constraints = {}
        for flow_id in flow_ids:
            factor = epsilon[flow_id]
            application = legacy.true_state.application[flow_id]
            transport = legacy.true_state.transport[flow_id]
            constraint = legacy.true_state.constraints[flow_id]
            required = application.required_rate_mbps * factor
            flow_rates[flow_id] = required
            applications[flow_id] = replace(application, required_rate_mbps=required)
            transports[flow_id] = replace(transport, send_rate_mbps=transport.send_rate_mbps * factor)
            constraints[flow_id] = replace(constraint, desired_rate_mbps=constraint.desired_rate_mbps * factor)
            for profile in metadata["proposal_profiles"][flow_id]["application"]:
                if "application_rate_mbps" in profile["parameters"]:
                    profile["parameters"]["application_rate_mbps"] *= factor
            for profile in metadata["proposal_profiles"][flow_id]["transport"]:
                if "transport_rate_mbps" in profile["parameters"]:
                    profile["parameters"]["transport_rate_mbps"] *= factor
        metadata["flow_r_req_mbps"] = flow_rates
        environment = deepcopy(metadata["random_environment"])
        environment["distribution_version"] = self.distribution_version
        environment["epsilon_by_flow"] = epsilon
        environment_fingerprint = hashlib.sha256(
            json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        metadata["random_environment"] = environment
        metadata["environment_fingerprint"] = environment_fingerprint

        true_state = replace(
            legacy.true_state, scenario="demand_capacity_ratio_v3",
            application=applications, transport=transports,
            constraints=constraints, metadata=metadata,
        )
        observed_state = deepcopy(true_state)
        proposals = collect_layer_proposals(observed_state)
        ground_truth = solve_truth(deepcopy(true_state), deepcopy(proposals))
        snapshot_metadata = deepcopy(legacy.metadata)
        snapshot_metadata.update({
            "distribution_version": self.distribution_version,
            "demand_capacity_definition": "R_i = gamma * C_eff_i * epsilon_i",
            "epsilon_by_flow": epsilon,
            "flow_r_req_mbps": flow_rates,
            "environment_fingerprint": environment_fingerprint,
            "random_environment": environment,
            "true_state_schema_version": self.true_state_schema_version,
            "observed_state_schema_version": self.observed_state_schema_version,
        })
        return RobustnessScenarioSnapshot(
            seed=seed, experiment="demand_capacity_ratio_v3",
            scenario="demand_capacity_ratio_v3", pressure=float(gamma),
            base=legacy.base, true_state=true_state, observed_state=observed_state,
            proposals=proposals, ground_truth=ground_truth, metadata=snapshot_metadata,
        )
