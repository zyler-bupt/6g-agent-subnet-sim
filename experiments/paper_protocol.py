from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import yaml


class RunMode(str, Enum):
    PILOT = "pilot"
    PAPER = "paper"


@dataclass(frozen=True)
class ModeSpec:
    topology_seeds: int
    events_per_seed: int
    rate_events_per_seed: int

    def trials_per_point(self, *, rate_metric: bool) -> int:
        event_count = self.rate_events_per_seed if rate_metric else self.events_per_seed
        return self.topology_seeds * event_count


_MODE_SPECS = {
    RunMode.PILOT: ModeSpec(
        topology_seeds=5,
        events_per_seed=2,
        rate_events_per_seed=2,
    ),
    RunMode.PAPER: ModeSpec(
        topology_seeds=30,
        events_per_seed=1,
        rate_events_per_seed=5,
    ),
}


# Formal figures must not imply a topology-cluster confidence interval when an
# exact x value was observed in only a handful of topologies.  Pilot figures
# use a smaller threshold because they contain only five topology seeds.
PILOT_FIGURE_MIN_TOPOLOGY_CLUSTERS = 1
PAPER_FIGURE_MIN_TOPOLOGY_CLUSTERS = 10


EXPECTED_PAPER_CONFIG: Mapping[str, Any] = {
    "mode": {
        "pilot": {"topology_seeds": 5, "events_per_seed": 2},
        "paper": {
            "topology_seeds": 30,
            "continuous_events_per_seed": 1,
            "rate_events_per_seed": 5,
        },
    },
    "topology": {
        "link_delay_ms": [5.0, 30.0],
        "link_bandwidth_mbps": [50.0, 200.0],
        "link_loss_percent": [0.0, 1.0],
        "link_jitter_ms": [0.0, 5.0],
    },
    "exp1": {
        "task_sizes": [8, 12, 16, 20, 24, 28, 32],
        "churn_percent": [0, 5, 10, 15, 20, 30],
        "churn_task_size": 24,
        "num_gateways": 12,
        "average_degree": [3.0, 4.0],
        "average_out_degree": [1.5, 2.0],
        "cross_gateway_edge_ratio": [0.60, 0.70],
    },
    "exp2": {
        "num_agents": 20,
        "num_gateways": 10,
        "dag_edges": [28, 32],
        "conflict_density_percent": [0, 10, 20, 30, 40, 50, 60],
        "conflict_type_weights": {
            "application_network": 0.25,
            "transport_network": 0.20,
            "network_physical": 0.20,
            "cascaded_multi_layer": 0.35,
        },
        "target_solvable_ratio": [0.85, 0.90],
    },
    "exp3": {
        "num_agents": 24,
        "num_gateways": 12,
        "target_dag_edges": 36,
        "affected_scope_percent": [10, 20, 30, 40, 50],
        "business_change_types": [
            "agent_add",
            "agent_remove",
            "dag_edge_change",
            "qos_update",
        ],
    },
    "exp4": {
        "num_agents": 24,
        "num_gateways": 12,
        "target_dag_edges": 36,
        "fixed_capacity_reduction_percent": 30,
        "capacity_reduction_percent": [10, 20, 30, 40, 50],
        "replacement_candidates": [1, 3],
    },
}


def mode_spec(mode: str | RunMode) -> ModeSpec:
    return _MODE_SPECS[RunMode(mode)]


def figure_min_topology_clusters(mode: str | RunMode) -> int:
    return (
        PILOT_FIGURE_MIN_TOPOLOGY_CLUSTERS
        if RunMode(mode) is RunMode.PILOT
        else PAPER_FIGURE_MIN_TOPOLOGY_CLUSTERS
    )


def load_and_validate_paper_config(
    path: str | Path = Path("configs/paper_experiments.yaml"),
) -> dict[str, Any]:
    """Load the protocol reference and reject silent config/code drift.

    Runners still permit explicit reduced grids for unit tests, but an
    authoritative pilot/paper invocation must be tied to this reviewed
    protocol.  Actual runtime grids are additionally recorded in manifests.
    """

    config_path = Path(path)
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError("paper protocol drift: configuration is not a mapping")
    if stable_fingerprint(loaded) != stable_fingerprint(EXPECTED_PAPER_CONFIG):
        raise RuntimeError(
            "paper protocol drift: configs/paper_experiments.yaml no longer "
            "matches the implemented and reviewed experiment protocol"
        )
    return loaded


@dataclass(frozen=True)
class MethodMetadata:
    method_id: str
    label: str
    source: str
    adapted: bool


def _method(
    method_id: str,
    label: str,
    source: str,
    *,
    adapted: bool = False,
) -> MethodMetadata:
    return MethodMetadata(method_id, label, source, adapted)


METHODS: Mapping[str, MethodMetadata] = {
    "proposed": _method("proposed", "Proposed", "This work"),
    "proposed_without_batch": _method(
        "proposed_without_batch",
        "Proposed w/o Batch",
        "This work",
    ),
    "cspf": _method("cspf", "CSPF", "CSPF"),
    "srd": _method(
        "srd",
        "SRD",
        "Sequential Rule Deployment",
    ),
    "ilp_sfc": _method("ilp_sfc", "ILP-SFC", "ILP SFC/VNF Embedding"),
    "sfc_reoptimization": _method(
        "sfc_reoptimization",
        "SFC Re-opt",
        "SFC Re-optimization",
    ),
    "sanet_dw": _method("sanet_dw", "SANet*", "SANet", adapted=True),
    "adjacent_layer": _method(
        "adjacent_layer",
        "Adjacent-Layer",
        "Adjacent-layer coordination",
    ),
    "independent": _method(
        "independent",
        "Independent",
        "Independent layer optimization",
    ),
    "weighted_sum": _method(
        "weighted_sum",
        "Weighted-Sum",
        "Weighted sum multi-objective",
    ),
    "netren": _method("netren", "NetRen*", "NetRen", adapted=True),
    "sfc_reconfiguration": _method(
        "sfc_reconfiguration",
        "SFC-Reconfig*",
        "SFC Reconfiguration",
        adapted=True,
    ),
    "local_only": _method("local_only", "Local-Only", "Local repair"),
    "full_rebuild": _method(
        "full_rebuild",
        "Full Rebuild",
        "Global reconstruction",
    ),
    "netkeeper": _method(
        "netkeeper",
        "NetKeeper*",
        "NetKeeper",
        adapted=True,
    ),
    "frr": _method("frr", "FRR", "Fast Reroute"),
    "te_reopt": _method("te_reopt", "TE-Reopt", "Traffic Engineering Reopt"),
    "sfc_restoration": _method(
        "sfc_restoration",
        "SFC-Restore",
        "SFC Restoration",
    ),
}


# Frozen protocol for WCNC 2027 (experiment revision v2).
#
# Each experiment answers ONE scientific question; every baseline has a clear
# paper-level role. Do NOT add further main baselines without motivation.
#   Exp1 Formation   : proposed vs CSPF + SFC Re-optimization
#   Exp2 Coordination: proposed vs Independent / Weighted-Sum / SANet
#   Exp3 Elasticity  : proposed vs Local-Only / NetRen / Full Rebuild
#   Exp4 Recovery    : heterogeneous failures, compared PER failure type
#                       (see EXPERIMENT_FAILURE_METHODS) -- never all together.
EXPERIMENT_METHODS: Mapping[str, tuple[str, ...]] = {
    "exp1": ("proposed", "cspf", "sfc_reoptimization"),
    "exp2": ("proposed", "independent", "weighted_sum", "sanet_dw"),
    "exp3": ("proposed", "local_only", "netren", "full_rebuild"),
    # Executable union of recovery strategies currently implemented. The
    # plotting/aggregation layer groups these per failure type using
    # EXPERIMENT_FAILURE_METHODS. cspf / FRR / TE-Reopt recovery strategies
    # belong to the frozen protocol but are not yet implemented (see audit).
    "exp4": ("proposed", "full_rebuild", "network_only"),
}


# v2 Exp4: failure-specific comparison sets (the paper protocol). These name the
# baselines each failure type is judged against; the runner executes
# EXPERIMENT_METHODS["exp4"] and the plotting layer filters per failure type.
#   LINK_FAILURE           : proposed vs CSPF (+ optional FRR)
#   AGENT_FAILURE          : proposed vs Full Rebuild
#   PHYSICAL_CAPACITY_DROP : proposed vs TE-Reopt + Full Rebuild
EXPERIMENT_FAILURE_METHODS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "exp4": {
        "LINK_FAILURE": ("proposed", "cspf", "frr"),
        "AGENT_FAILURE": ("proposed", "full_rebuild"),
        "PHYSICAL_CAPACITY_DROP": ("proposed", "te_reopt", "full_rebuild"),
    }
}


def stable_fingerprint(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
