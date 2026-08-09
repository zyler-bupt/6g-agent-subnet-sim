from __future__ import annotations

import hashlib
import json
import random
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from src.agents.layer_proposals import collect_layer_proposals
from src.controller.ground_truth import GroundTruthResult, GroundTruthSolver
from src.core.cross_layer import CrossLayerTaskState, LayerProposal
from src.core.models import to_jsonable
from src.simulation.conflict_scenario_generator import (
    ConflictScenarioConfig,
    ConflictScenarioGenerator,
    ConflictScenarioSnapshot,
)


NO_CONFLICT = "NO_CONFLICT"
RESOLVABLE_CONFLICT = "RESOLVABLE_CONFLICT"
UNRESOLVABLE_CONFLICT = "UNRESOLVABLE_CONFLICT"

UNRESOLVABLE_CASES = (
    "physical_capacity_absolute",
    "all_routes_latency",
    "reliability_impossible",
    "shared_resource_shortage",
)

NOISE_FIELDS = (
    "application_required_rate",
    "transport_available_rate",
    "network_available_bandwidth",
    "network_latency",
    "physical_available_capacity",
    "physical_reliability",
)


@dataclass(frozen=True)
class RobustnessScenarioSnapshot:
    seed: int
    experiment: str
    scenario: str
    pressure: float
    base: ConflictScenarioSnapshot
    true_state: CrossLayerTaskState
    observed_state: CrossLayerTaskState
    proposals: tuple[LayerProposal, ...]
    ground_truth: GroundTruthResult
    metadata: dict[str, Any]

    @property
    def conflict_class(self) -> str:
        return classify_ground_truth(self.ground_truth)

    @property
    def fingerprint(self) -> str:
        payload = {
            "seed": self.seed,
            "experiment": self.experiment,
            "scenario": self.scenario,
            "pressure": self.pressure,
            "base_fingerprint": self.base.fingerprint,
            "true_state": to_jsonable(self.true_state),
            "observed_state": to_jsonable(self.observed_state),
            "proposals": to_jsonable(self.proposals),
            "metadata": to_jsonable(self.metadata),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def instantiate(self):
        return self.base.instantiate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "experiment": self.experiment,
            "scenario": self.scenario,
            "pressure": self.pressure,
            "conflict_class": self.conflict_class,
            "scenario_fingerprint": self.fingerprint,
            "base_fingerprint": self.base.fingerprint,
            "true_state": to_jsonable(self.true_state),
            "observed_state": to_jsonable(self.observed_state),
            "proposals": to_jsonable(self.proposals),
            "ground_truth": to_jsonable(self.ground_truth),
            "metadata": to_jsonable(self.metadata),
        }


class ConflictRobustnessGenerator:
    """Construct paired robustness cases without touching Exp2 snapshots."""

    def __init__(self) -> None:
        self._base = ConflictScenarioGenerator()
        self._ground_truth = GroundTruthSolver()

    def base_case(
        self,
        scenario: str,
        pressure: float,
        seed: int,
        config: ConflictScenarioConfig | None = None,
    ) -> RobustnessScenarioSnapshot:
        base = self._base.generate(scenario, pressure, seed, config)
        return self._snapshot(
            base=base,
            experiment="scenario_audit",
            scenario=scenario,
            true_state=base.cross_layer_state,
            observed_state=base.cross_layer_state,
            proposals=base.proposals,
            metadata={"observation_source": "stable_true_state"},
        )

    def unresolvable(
        self,
        case: str,
        seed: int,
        config: ConflictScenarioConfig | None = None,
    ) -> RobustnessScenarioSnapshot:
        if case not in UNRESOLVABLE_CASES:
            raise ValueError(f"unsupported unresolvable case: {case}")
        scenario = {
            "physical_capacity_absolute": "application_capacity",
            "all_routes_latency": "network_physical",
            "reliability_impossible": "network_physical",
            "shared_resource_shortage": "application_capacity",
        }[case]
        base = self._base.generate(scenario, 1.1, seed, config)
        state = _make_unresolvable(base.cross_layer_state, case)
        proposals = collect_layer_proposals(state)
        snapshot = self._snapshot(
            base=base,
            experiment="unresolvable",
            scenario=case,
            true_state=state,
            observed_state=state,
            proposals=proposals,
            metadata={
                "unresolvable_reason": case,
                "observation_source": "stable_true_state",
            },
        )
        if snapshot.conflict_class != UNRESOLVABLE_CONFLICT:
            raise AssertionError(f"{case} unexpectedly has a feasible combination")
        return snapshot

    def noisy(
        self,
        scenario: str,
        pressure: float,
        noise_ratio: float,
        seed: int,
        config: ConflictScenarioConfig | None = None,
    ) -> RobustnessScenarioSnapshot:
        base = self._base.generate(scenario, pressure, seed, config)
        observed, errors = perturb_observation(
            base.cross_layer_state,
            noise_ratio,
            seed,
        )
        proposals = collect_layer_proposals(observed)
        return self._snapshot(
            base=base,
            experiment="noise",
            scenario=scenario,
            true_state=base.cross_layer_state,
            observed_state=observed,
            proposals=proposals,
            metadata={
                "noise_ratio": noise_ratio,
                "noise_model": "multiplicative_independent_gaussian_clipped",
                "noise_errors": errors,
                "ground_truth_state": "true_state",
            },
        )

    def stale(
        self,
        stale_ms: int,
        seed: int,
        config: ConflictScenarioConfig | None = None,
    ) -> RobustnessScenarioSnapshot:
        base = self._base.generate("application_capacity", 1.0, seed, config)
        proposals = base.proposals
        true_state = evolve_after_observation(base.cross_layer_state, stale_ms)
        return self._snapshot(
            base=base,
            experiment="stale",
            scenario="stale_state",
            true_state=true_state,
            observed_state=true_state,
            proposals=proposals,
            metadata={
                "stale_ms": stale_ms,
                "proposal_generated_version": 1,
                "execution_version": int(
                    true_state.metadata.get("stable_version", 1)
                ),
                "read_set_version": 1,
                "proposal_regenerated": False,
            },
        )

    def missing_layer(
        self,
        missing_layer: str,
        seed: int,
        config: ConflictScenarioConfig | None = None,
    ) -> RobustnessScenarioSnapshot:
        if missing_layer not in {
            "application",
            "transport",
            "network",
            "physical",
        }:
            raise ValueError(f"unsupported missing layer: {missing_layer}")
        base = self._base.generate("application_capacity", 1.2, seed, config)
        proxy = conservative_missing_layer_proxy(
            base.cross_layer_state,
            missing_layer,
        )
        proposals = tuple(
            proposal
            for proposal in collect_layer_proposals(proxy)
            if proposal.layer != missing_layer
        )
        return self._snapshot(
            base=base,
            experiment="missing_layer",
            scenario=f"missing_{missing_layer}",
            true_state=base.cross_layer_state,
            observed_state=proxy,
            proposals=proposals,
            ground_truth_proposals=base.proposals,
            metadata={
                "missing_layer": missing_layer,
                "missing_value_policy": "conservative_bound",
                "ground_truth_state": "true_state_with_complete_observations",
            },
        )

    def proposal_scale(
        self,
        proposals_per_layer: int,
        seed: int,
        config: ConflictScenarioConfig | None = None,
    ) -> RobustnessScenarioSnapshot:
        if proposals_per_layer not in {1, 2, 3, 4, 5}:
            raise ValueError("proposals_per_layer must be between 1 and 5")
        base = self._base.generate("application_capacity", 1.2, seed, config)
        proposals = resize_proposal_pool(base.proposals, proposals_per_layer)
        return self._snapshot(
            base=base,
            experiment="proposal_scale",
            scenario="proposal_scale",
            true_state=base.cross_layer_state,
            observed_state=base.cross_layer_state,
            proposals=proposals,
            metadata={
                "proposals_per_layer": proposals_per_layer,
                "num_raw_combinations": proposals_per_layer**4,
            },
        )

    def _snapshot(
        self,
        *,
        base: ConflictScenarioSnapshot,
        experiment: str,
        scenario: str,
        true_state: CrossLayerTaskState,
        observed_state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
        metadata: dict[str, Any],
        ground_truth_proposals: tuple[LayerProposal, ...] | None = None,
    ) -> RobustnessScenarioSnapshot:
        oracle_state = deepcopy(true_state)
        oracle_proposals = deepcopy(ground_truth_proposals or proposals)
        ground_truth = self._ground_truth.solve(
            oracle_state,
            oracle_proposals,
        )
        return RobustnessScenarioSnapshot(
            seed=base.seed,
            experiment=experiment,
            scenario=scenario,
            pressure=base.pressure,
            base=base,
            true_state=deepcopy(true_state),
            observed_state=deepcopy(observed_state),
            proposals=deepcopy(proposals),
            ground_truth=ground_truth,
            metadata=metadata,
        )


def classify_ground_truth(result: GroundTruthResult) -> str:
    if not result.ground_truth_conflict:
        return NO_CONFLICT
    if result.ground_truth_resolvable:
        return RESOLVABLE_CONFLICT
    return UNRESOLVABLE_CONFLICT


def perturb_observation(
    state: CrossLayerTaskState,
    sigma: float,
    seed: int,
) -> tuple[CrossLayerTaskState, dict[str, float]]:
    if sigma < 0.0:
        raise ValueError("noise ratio cannot be negative")
    rng = random.Random(
        f"exp2-robust-noise:{state.scenario}:{state.pressure}:{seed}:{sigma}"
    )
    errors = {field: rng.gauss(0.0, sigma) for field in NOISE_FIELDS}
    application = {
        edge_id: replace(
            item,
            required_rate_mbps=_positive(
                item.required_rate_mbps
                * (1.0 + errors["application_required_rate"])
            ),
        )
        for edge_id, item in state.application.items()
    }
    transport = {
        edge_id: replace(
            item,
            send_rate_mbps=_positive(
                item.send_rate_mbps
                * (1.0 + errors["transport_available_rate"])
            ),
        )
        for edge_id, item in state.transport.items()
    }
    network = {
        edge_id: replace(
            item,
            available_bandwidth_mbps=_positive(
                item.available_bandwidth_mbps
                * (1.0 + errors["network_available_bandwidth"])
            ),
            latency_ms=_positive(
                item.latency_ms * (1.0 + errors["network_latency"])
            ),
        )
        for edge_id, item in state.network.items()
    }
    physical = {
        edge_id: replace(
            item,
            available_capacity_mbps=_positive(
                item.available_capacity_mbps
                * (1.0 + errors["physical_available_capacity"])
            ),
            reliability=_unit(
                item.reliability * (1.0 + errors["physical_reliability"])
            ),
        )
        for edge_id, item in state.physical.items()
    }
    return (
        replace(
            state,
            application=application,
            transport=transport,
            network=network,
            physical=physical,
            metadata={
                **state.metadata,
                "observation_noise_sigma": sigma,
                "observation_noise_errors": errors,
            },
        ),
        errors,
    )


def evolve_after_observation(
    state: CrossLayerTaskState,
    stale_ms: int,
) -> CrossLayerTaskState:
    if stale_ms < 0:
        raise ValueError("stale time cannot be negative")
    if stale_ms == 0:
        return state
    degradation = min(0.45, stale_ms / 500.0)
    network = {
        edge_id: replace(
            item,
            available_bandwidth_mbps=max(
                0.1,
                item.available_bandwidth_mbps * (1.0 - degradation),
            ),
            latency_ms=item.latency_ms * (1.0 + degradation),
            utilization=min(0.99, item.utilization + degradation / 2.0),
        )
        for edge_id, item in state.network.items()
    }
    physical = {
        edge_id: replace(
            item,
            available_capacity_mbps=max(
                0.1,
                item.available_capacity_mbps * (1.0 - degradation),
            ),
            reliability=max(0.0, item.reliability - degradation * 0.03),
        )
        for edge_id, item in state.physical.items()
    }
    return replace(
        state,
        network=network,
        physical=physical,
        metadata={
            **state.metadata,
            "stable_version": 2,
            "state_changed_after_observation": True,
            "stale_ms": stale_ms,
        },
    )


def conservative_missing_layer_proxy(
    state: CrossLayerTaskState,
    missing_layer: str,
) -> CrossLayerTaskState:
    if missing_layer == "application":
        return replace(
            state,
            application={
                edge_id: replace(
                    item,
                    required_rate_mbps=(
                        state.constraints[edge_id].desired_rate_mbps * 1.25
                    ),
                )
                for edge_id, item in state.application.items()
            },
        )
    if missing_layer == "transport":
        return replace(
            state,
            transport={
                edge_id: replace(item, send_rate_mbps=0.1, reliability=0.90)
                for edge_id, item in state.transport.items()
            },
        )
    if missing_layer == "network":
        return replace(
            state,
            network={
                edge_id: replace(
                    item,
                    available_bandwidth_mbps=0.1,
                    latency_ms=state.constraints[edge_id].max_latency_ms * 2.0,
                    reachable=False,
                    reliability=0.90,
                )
                for edge_id, item in state.network.items()
            },
        )
    if missing_layer == "physical":
        return replace(
            state,
            physical={
                edge_id: replace(
                    item,
                    available_capacity_mbps=0.1,
                    reliability=0.90,
                    online=False,
                )
                for edge_id, item in state.physical.items()
            },
        )
    raise ValueError(f"unsupported missing layer: {missing_layer}")


def resize_proposal_pool(
    proposals: tuple[LayerProposal, ...],
    proposals_per_layer: int,
) -> tuple[LayerProposal, ...]:
    layers = ("application", "transport", "network", "physical")
    output: list[LayerProposal] = []
    for layer in layers:
        group = sorted(
            (proposal for proposal in proposals if proposal.layer == layer),
            key=lambda item: (not item.is_keep, item.proposal_id),
        )
        if proposals_per_layer <= len(group):
            output.extend(group[:proposals_per_layer])
            continue
        output.extend(group)
        template = max(group, key=lambda item: (item.utility, item.proposal_id))
        for index in range(len(group), proposals_per_layer):
            output.append(
                replace(
                    template,
                    proposal_id=f"{template.proposal_id}:scale-{index}",
                    expected_qos_gain=template.expected_qos_gain + 0.01 * index,
                    expected_cost=template.expected_cost + 0.005 * index,
                )
            )
    return tuple(output)


def _make_unresolvable(
    state: CrossLayerTaskState,
    case: str,
) -> CrossLayerTaskState:
    edge_id = next(iter(state.constraints))
    profiles = {
        layer: tuple(dict(profile) for profile in values)
        for layer, values in state.metadata["proposal_profiles"][edge_id].items()
    }
    constraints = dict(state.constraints)
    shared_capacity = dict(state.shared_resource_capacity_mbps)
    metadata = dict(state.metadata)
    application = dict(state.application)
    transport = dict(state.transport)
    network = dict(state.network)
    physical = dict(state.physical)

    if case == "physical_capacity_absolute":
        required = 30.0
        application[edge_id] = replace(
            application[edge_id], required_rate_mbps=required
        )
        transport[edge_id] = replace(transport[edge_id], send_rate_mbps=required)
        network[edge_id] = replace(
            network[edge_id], available_bandwidth_mbps=35.0
        )
        physical[edge_id] = replace(
            physical[edge_id], available_capacity_mbps=18.0
        )
        constraints[edge_id] = replace(
            constraints[edge_id], desired_rate_mbps=required
        )
        profiles["application"] = _force_profile_parameter(
            profiles["application"], "application_rate_mbps", required
        )
        profiles["transport"] = _force_profile_parameter(
            profiles["transport"], "transport_rate_mbps", required
        )
        profiles["network"] = _cap_profile_parameter(
            profiles["network"], "network_bandwidth_mbps", 35.0
        )
        profiles["physical"] = _cap_profile_parameter(
            profiles["physical"], "physical_capacity_mbps", 20.0
        )
    elif case == "all_routes_latency":
        constraints[edge_id] = replace(constraints[edge_id], max_latency_ms=10.0)
        application[edge_id] = replace(application[edge_id], max_latency_ms=10.0)
        network[edge_id] = replace(network[edge_id], latency_ms=8.0)
        profiles["network"] = _floor_profile_parameter(
            profiles["network"], "network_latency_ms", 8.0
        )
    elif case == "reliability_impossible":
        constraints[edge_id] = replace(
            constraints[edge_id], min_reliability=0.99999
        )
        application[edge_id] = replace(
            application[edge_id], min_reliability=0.99999
        )
        profiles["transport"] = _cap_profile_parameter(
            profiles["transport"], "transport_reliability", 0.999
        )
        profiles["network"] = _cap_profile_parameter(
            profiles["network"], "network_reliability", 0.999
        )
        profiles["physical"] = _cap_profile_parameter(
            profiles["physical"], "physical_reliability", 0.999
        )
    elif case == "shared_resource_shortage":
        resource_id = constraints[edge_id].shared_resource_id
        shared_capacity[resource_id] = 15.0
        metadata["shared_resource_background_demand_mbps"] = {
            resource_id: 12.0
        }
        profiles["transport"] = _floor_profile_parameter(
            profiles["transport"], "transport_rate_mbps", 10.0
        )
    else:
        raise ValueError(case)

    proposal_profiles = dict(metadata["proposal_profiles"])
    proposal_profiles[edge_id] = profiles
    metadata["proposal_profiles"] = proposal_profiles
    metadata["unresolvable_reason"] = case
    return replace(
        state,
        scenario=case,
        application=application,
        transport=transport,
        network=network,
        physical=physical,
        constraints=constraints,
        shared_resource_capacity_mbps=shared_capacity,
        metadata=metadata,
    )


def _force_profile_parameter(
    profiles: tuple[dict[str, object], ...],
    field: str,
    value: float,
) -> tuple[dict[str, object], ...]:
    return tuple(
        _replace_profile_parameter(profile, field, value)
        for profile in profiles
    )


def _cap_profile_parameter(
    profiles: tuple[dict[str, object], ...],
    field: str,
    maximum: float,
) -> tuple[dict[str, object], ...]:
    output = []
    for profile in profiles:
        parameters = dict(profile.get("parameters", {}))
        current = float(parameters.get(field, maximum))
        output.append(
            _replace_profile_parameter(profile, field, min(current, maximum))
        )
    return tuple(output)


def _floor_profile_parameter(
    profiles: tuple[dict[str, object], ...],
    field: str,
    minimum: float,
) -> tuple[dict[str, object], ...]:
    output = []
    for profile in profiles:
        parameters = dict(profile.get("parameters", {}))
        current = float(parameters.get(field, minimum))
        output.append(
            _replace_profile_parameter(profile, field, max(current, minimum))
        )
    return tuple(output)


def _replace_profile_parameter(
    profile: dict[str, object],
    field: str,
    value: float,
) -> dict[str, object]:
    copy = dict(profile)
    copy["parameters"] = {
        **dict(profile.get("parameters", {})),
        field: value,
    }
    return copy


def _positive(value: float) -> float:
    return max(0.001, value)


def _unit(value: float) -> float:
    return min(1.0, max(0.0, value))
