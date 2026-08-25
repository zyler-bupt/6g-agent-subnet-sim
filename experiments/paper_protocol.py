from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import yaml


PROTOCOL_ID = "wcnc_final_v3"


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


EXPECTED_WCNC_V3_CONFIG: Mapping[str, Any] = {
    "protocol": {
        "id": PROTOCOL_ID,
        "frozen": True,
        "canonical_root": "results/paper/wcnc_final_v3",
    },
    "pilot": {"seeds": "9000:9019"},
    "formal": {"exp1_seeds": "0:49", "exp2_exp4_seeds": "0:99"},
    "exp1": {
        "formal_config": "configs/exp1_netns_verified_formation_v3.yaml",
        "pilot_config": "configs/exp1_netns_verified_formation_pilot_v3.yaml",
        "num_agents": [4, 8, 12, 16, 20],
        "methods": ["proposed", "cspf", "global_sfc_embedding"],
        "deployment_policies": {
            "proposed": "parallel_single_batch",
            "cspf": "sequential_flow_batches",
            "global_sfc_embedding": "sequential_hop_route_then_activation_batches",
        },
        "require_isolated_outer_network_namespace": True,
        "require_all_formal_trials_successful": True,
        "timeout_s": 45,
        "result_mode": "measured_netns",
    },
    "exp2": {
        "gamma": [0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3],
        "methods": ["proposed", "sanet_dw", "weighted_sum", "independent"],
        "epsilon_min": 0.85,
        "epsilon_max": 1.15,
        "epsilon_distribution": "bounded_centered_seed_fixed",
        "max_combinations": 4096,
        "coordination_timeout_ms": 10000,
        "result_mode": "transactional_simulation",
    },
    "exp3": {
        "affected_dependency_scope_percent": [10, 20, 30, 40, 50],
        "methods": ["proposed", "netren", "local_only", "full_rebuild"],
        "result_mode": "transactional_simulation",
    },
    "exp4": {
        "link_affected_flow_ratio": [0.03, 0.06, 0.1, 0.15, 0.25],
        "agent_dependency_closure_ratio": [0.1, 0.2, 0.3, 0.4, 0.5],
        "capacity_ratio": [1.1, 1.0, 0.9, 0.75, 0.6],
        "methods_by_failure": {
            "link_failure": ["proposed", "cspf", "full_rebuild"],
            "agent_failure": ["proposed", "sfc_restoration", "full_rebuild"],
            "capacity_degradation": ["proposed", "te_reopt", "full_rebuild"],
        },
        "result_mode": "transactional_simulation",
    },
    "statistics": {
        "bootstrap_iterations": 5000,
        "cluster": "seed",
        "rate_interval": "wilson_95",
        "continuous_interval": "topology_cluster_bootstrap_95",
        "comparison_interval": "paired_topology_cluster_bootstrap_95",
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


def load_and_validate_wcnc_v3_config(
    path: str | Path = Path("configs/wcnc_final_v3.yaml"),
) -> dict[str, Any]:
    config_path = Path(path)
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError("wcnc_final_v3 protocol drift: configuration is not a mapping")
    if stable_fingerprint(loaded) != stable_fingerprint(EXPECTED_WCNC_V3_CONFIG):
        raise RuntimeError(
            "wcnc_final_v3 protocol drift: configs/wcnc_final_v3.yaml no longer "
            "matches the frozen canonical protocol"
        )
    return loaded


@dataclass(frozen=True)
class MethodMetadata:
    """Per-method metadata required by the WCNC-2027 final protocol (v3 §6).

    Baselines are explicitly classified so the paper never presents a
    strategy/ablation as if it were a published algorithm, and every method
    has a stated scientific role.
    """

    method_id: str
    label: str
    # "proposed" | "literature-inspired" | "strategy" | "internal-ablation"
    category: str
    # what the method represents / source line (published work or internal)
    reference: str
    # paper-level scientific role: why this method is included
    why_included: str
    # where it appears in the paper: "main" | "appendix" | "ablation" | "pending"
    status: str = "main"
    adapted: bool = False
    experiment_labels: Mapping[str, str] = field(default_factory=dict)


def _method(
    method_id: str,
    label: str,
    category: str,
    reference: str,
    why_included: str,
    *,
    status: str = "main",
    adapted: bool = False,
    experiment_labels: Mapping[str, str] | None = None,
) -> MethodMetadata:
    return MethodMetadata(
        method_id,
        label,
        category,
        reference,
        why_included,
        status,
        adapted,
        dict(experiment_labels or {}),
    )


# category legend
#   proposed            -> the proposed task-driven cross-layer framework
#   literature-inspired -> inspired by a published work / classic algorithm
#   strategy            -> a repair/reconstruction strategy (NOT a published algo)
#   internal-ablation   -> internal ablation study, never a main-figure baseline
# status legend
#   main      -> appears in the frozen main protocol figures
#   appendix  -> scalability / supplementary analysis only
#   ablation  -> internal ablation, excluded from main figures
#   pending   -> named in the frozen protocol but recovery strategy not yet
#                implemented; runner uses an executable placeholder
METHODS: Mapping[str, MethodMetadata] = {
    "proposed": _method(
        "proposed", "Ours", "proposed",
        "This work (task-driven cross-layer agent subnet)",
        "Task-aware cross-layer subnet compilation with verified execution "
        "and parallel gateway deployment. The method under evaluation.",
    ),
    "proposed_without_batch": _method(
        "proposed_without_batch", "Proposed w/o Batch", "internal-ablation",
        "This work (ablation)",
        "Ablation: removes parallel batch deployment to isolate its "
        "contribution. Internal study only, not in main figures.",
        status="ablation",
    ),
    "cspf": _method(
        "cspf", "CSPF Recovery", "literature-inspired",
        "Constraint-based Shortest Path First (RFC 2702 / MPLS-TE)",
        "Network-centric constrained path computation. Represents the "
        "traditional routing-oriented solution for Exp1.",
        experiment_labels={
            "exp1": "CSPF-based Formation",
            "exp4": "CSPF Recovery",
        },
    ),
    "srd": _method(
        "srd", "SRD", "internal-ablation",
        "Sequential Rule Deployment (internal)",
        "Ablation of the deployment strategy. Not a literature baseline; "
        "internal study only.",
        status="ablation",
    ),
    "ilp_sfc": _method(
        "ilp_sfc", "ILP-SFC", "literature-inspired",
        "ILP VNF/SFC embedding (e.g. Gubichev et al.)",
        "Global optimization reference for a scalability analysis. Does not "
        "model task-agent semantic dependency, so kept in appendix only.",
        status="appendix",
    ),
    "sfc_reoptimization": _method(
        "sfc_reoptimization", "SFC Re-opt", "literature-inspired",
        "SFC Re-optimization (service-chain re-embedding)",
        "Complete service-chain reconstruction: recomputes path, rule set and "
        "deployment order on every change. Exp1 baseline for full recompute.",
    ),
    "global_sfc_embedding": _method(
        "global_sfc_embedding",
        "Global SFC Embedding (Heuristic)",
        "strategy",
        "Deterministic global service-chain embedding heuristic",
        "Converts Task-DAG source-to-sink paths to service chains and jointly "
        "selects healthy Agent placements and constrained network paths.",
    ),
    "sanet_dw": _method(
        "sanet_dw", "SANet-DW*", "literature-inspired",
        "SANet-inspired Dynamic-Weight Coordination (adapted); "
        "IEEE TMC 2026, DOI 10.1109/TMC.2026.3691804; upstream "
        "60d9b3c1db02aa2018e67b0020a0feb57d9e3d73",
        "Closest existing semantic-aware agent coordination work. Literature "
        "baseline for Exp2 cross-layer coordination.",
        adapted=True,
    ),
    "adjacent_layer": _method(
        "adjacent_layer", "Adjacent-Layer", "internal-ablation",
        "Adjacent-layer coordination (internal)",
        "Internal ablation only: weaker than global coordination and not a "
        "strong literature baseline, so excluded from main figures.",
        status="ablation",
    ),
    "independent": _method(
        "independent", "Independent Layer Optimization", "strategy",
        "Independent layer optimization (oracle baseline)",
        "Each layer optimized independently with no cross-layer interaction. "
        "Shows why coordination is necessary (Exp2).",
    ),
    "weighted_sum": _method(
        "weighted_sum", "Weighted-Sum Coordination", "strategy",
        "Weighted-sum multi-objective optimization",
        "Classic soft cross-layer optimization baseline for Exp2: maximizes a "
        "weighted utility without hard feasibility verification.",
    ),
    "netren": _method(
        "netren", "NetRen*", "literature-inspired",
        "NetRen: Service Migration-Driven Network Renascence with Synthesizing "
        "Updated Configuration (ASPLOS 2024), adapted; "
        "DOI 10.1145/3620666.3651365",
        "Represents dynamic service/network reconfiguration. Literature "
        "baseline for Exp3 elastic reconfiguration.",
        adapted=True,
    ),
    "sfc_reconfiguration": _method(
        "sfc_reconfiguration", "SFC-Reconfig*", "literature-inspired",
        "SFC Reconfiguration, adapted",
        "Service-chain reconfiguration baseline for an appendix/alternative "
        "elasticity comparison.",
        adapted=True, status="appendix",
    ),
    "local_only": _method(
        "local_only", "Local-Only", "strategy",
        "Local-only update (repair strategy)",
        "Fast local repair that modifies only directly affected components. "
        "May miss global dependencies. Strategy baseline for Exp3.",
    ),
    "full_rebuild": _method(
        "full_rebuild", "Full Rebuild", "strategy",
        "Full rebuild (global reconstruction)",
        "Recomputes the whole subnet from scratch. High correctness but "
        "expensive. Strategy baseline for Exp3 and Exp4 (agent failure).",
    ),
    "network_only": _method(
        "network_only", "Network-Only", "strategy",
        "Network-only local recovery (legacy proxy)",
        "Former executable proxy for link/capacity recovery baselines. "
        "Replaced by the failure-specific CSPF / TE-Reopt strategies in the "
        "frozen protocol; kept only as an internal-ablation reference.",
        status="ablation",
    ),
    "netkeeper": _method(
        "netkeeper", "NetKeeper*", "literature-inspired",
        "NetKeeper, adapted",
        "Network-keeper style baseline for an alternative recovery comparison; "
        "not in the frozen main figures.",
        adapted=True, status="appendix",
    ),
    "frr": _method(
        "frr", "FRR", "literature-inspired",
        "IP/MPLS Fast Reroute",
        "Optional local-protection baseline for link-failure recovery (Exp4). "
        "Omitted from the main protocol because it is a near-duplicate of CSPF "
        "for our path-recovery scenario; kept registered for reference.",
        status="appendix",
    ),
    "te_reopt": _method(
        "te_reopt", "TE Re-optimization", "strategy",
        "Traffic Engineering Re-optimization",
        "TE re-optimization baseline for physical-capacity-degradation "
        "recovery (Exp4). Redistributes network resources without task-DAG "
        "semantic awareness, so it may modify unnecessary paths.",
    ),
    "sfc_restoration": _method(
        "sfc_restoration", "SFC Restoration", "strategy",
        "SFC Restoration",
        "Service-chain restoration baseline for agent-failure recovery (Exp4). "
        "Replaces the failed function and rebuilds the chain around it; correct "
        "but cannot exploit task-DAG dependency, so larger scope than Proposed.",
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
    "exp1": ("proposed", "cspf", "global_sfc_embedding"),
    "exp2": ("proposed", "sanet_dw", "weighted_sum", "independent"),
    "exp3": ("proposed", "netren", "local_only", "full_rebuild"),
    # Executable union of recovery strategies currently implemented (failure
    # type specific). The runner iterates EXPERIMENT_FAILURE_METHODS[exp4]
    # per failure type; this tuple is only a convenience for callers that need
    # the full implemented set. Every entry here must be dispatchable by
    # run_paper_failure_method.
    "exp4": ("proposed", "cspf", "full_rebuild", "sfc_restoration", "te_reopt"),
}


# Frozen Exp4 (v3 final alignment): failure-specific comparison sets. The keys
# match PaperFailureSnapshot.failure_type EXACTLY so the runner can index with
# snapshot.failure_type directly. Each failure type is judged against its own
# representative literature-inspired recovery method(s); baselines are never
# mixed across failure types.
#   link_failure         : proposed vs CSPF (routing/path recovery)
#   agent_failure         : proposed vs SFC-Restoration vs Full Rebuild
#                           (service/task recovery; cross-layer awareness wins)
#   capacity_degradation  : proposed vs TE-Reopt vs Full Rebuild
#                           (resource reoptimization)
# FRR is intentionally omitted: it is an optional link baseline and would be a
# near-duplicate of CSPF for our path-recovery scenario (see audit).
EXPERIMENT_FAILURE_METHODS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "exp4": {
        "link_failure": ("proposed", "cspf", "full_rebuild"),
        "agent_failure": ("proposed", "sfc_restoration", "full_rebuild"),
        "capacity_degradation": ("proposed", "te_reopt", "full_rebuild"),
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
