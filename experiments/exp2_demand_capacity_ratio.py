from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import platform
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Collection, Mapping, Sequence

import yaml

from experiments.exp2_cross_layer_robustness import run_case
from src.core.models import to_jsonable
from src.metrics.exp2_robustness import write_jsonl
from src.simulation.conflict_scenario_generator import ConflictScenarioConfig
from src.simulation.demand_capacity_ratio import DemandCapacityRatioGenerator
from src.simulation.demand_capacity_truth import evaluate_truth


METHOD_TO_ENGINE = {
    "ours": "proposed",
    "alc": "adjacent",
    "layer_wise_independent": "independent",
    "no_global_verification": "no_verification",
}
FORMAL_GAMMA_GRID = (0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3)
FORMAL_SEEDS = tuple(range(100))
PILOT_GAMMA_GRID = (0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3, 1.4)
PILOT_SEEDS = tuple(range(9000, 9010))
FORMAL_OUTPUT = Path("results/exp2_demand_capacity_v5")
PILOT_OUTPUT = Path("results/wcnc_pilot_v2/exp2_demand_capacity_ratio_v5")
GENERATOR_VERSION = "wcnc-final-gamma-v5"
GAMMA_DEFINITION = (
    "gamma = R_req / min(C_t_admissible, C_n_available, C_p_available)"
)
RESULT_MODE = "in_memory_transactional_control_plane_gamma_simulation"
GROUND_TRUTH_CLASSES = {
    "NO_CONFLICT",
    "RESOLVABLE_CONFLICT",
    "UNRESOLVABLE_CONFLICT",
}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXECUTION_CRITICAL_SOURCES = (
    "src/core/cross_layer.py",
    "src/core/events.py",
    "src/core/gateway.py",
    "src/core/models.py",
    "src/core/rules.py",
    "src/agents/layer_proposals.py",
    "src/agents/base.py",
    "src/agents/forecast.py",
    "src/agents/phy_agent.py",
    "src/controller/conflicts.py",
    "src/controller/cross_layer_coordinator.py",
    "src/controller/feasibility.py",
    "src/controller/ground_truth.py",
    "src/controller/authorized_actions.py",
    "src/controller/impact.py",
    "src/controller/networking.py",
    "src/controller/reconfiguration.py",
    "src/controller/transaction_executor.py",
    "src/e2e/models.py",
    "src/e2e/transactional_installers.py",
    "src/e2e/verifiers.py",
    "src/metrics/exp2_robustness.py",
    "src/metrics/provider.py",
    "src/metrics/synthetic.py",
    "src/sim/topology.py",
    "src/simulation/conflict_robustness.py",
    "src/simulation/conflict_scenario_generator.py",
    "src/simulation/continuous_probe.py",
    "src/simulation/demand_capacity_truth.py",
    "src/simulation/demand_capacity_ratio.py",
    "src/simulation/scenario_generator.py",
    "experiments/exp2_cross_layer_robustness.py",
    "experiments/exp2_demand_capacity_ratio.py",
)


def validate_protocol(
    config: dict[str, Any],
    methods: Sequence[str],
    seeds: Sequence[int],
    output_dir: Path | None = None,
    *,
    validate_registered_seeds: bool = True,
) -> None:
    experiment = config.get("experiment", {})
    phase = experiment.get("phase")
    if phase not in {"formal", "pilot"}:
        raise ValueError("experiment.phase must be exactly formal or pilot")
    if phase == "formal" and experiment.get("frozen") is not True:
        raise ValueError("formal config is not frozen")
    expected_output = FORMAL_OUTPUT if phase == "formal" else PILOT_OUTPUT
    expected_seeds = FORMAL_SEEDS if phase == "formal" else PILOT_SEEDS
    expected_experiment = {
        "phase": phase,
        "frozen": True,
        "generator_version": GENERATOR_VERSION,
        "output_dir": str(expected_output),
    }
    if phase == "formal":
        expected_experiment["pilot_source"] = str(PILOT_OUTPUT)
    for key, expected in expected_experiment.items():
        if experiment.get(key) != expected:
            raise ValueError(f"{phase} experiment.{key} must be {expected!r}")
    if phase == "pilot" and any(
        key in experiment
        for key in ("pilot_source", "pilot_selection_path", "pilot_selection_sha256")
    ):
        raise ValueError("pilot experiment must not declare pilot_source")

    simulation = config.get("simulation", {})
    expected_simulation = {
        "seeds": len(expected_seeds),
        "seed_range": f"{expected_seeds[0]}:{expected_seeds[-1]}",
        "timeout_ms": 10000,
        "coordination_timeout_ms": 10000,
        "result_mode": RESULT_MODE,
    }
    for key, expected in expected_simulation.items():
        if simulation.get(key) != expected:
            raise ValueError(f"{phase} simulation.{key} must be {expected!r}")

    expected_task = {
        "num_agents": 10,
        "edge_ratio": 1.5,
        "num_gateways": 4,
        "cross_gateway_edge_ratio": 0.5,
    }
    if config.get("task") != expected_task:
        raise ValueError(f"{phase} task values must be frozen")

    demand = config.get("demand_capacity", {})
    expected_demand = {
        "definition": GAMMA_DEFINITION,
        "flows_per_task": 3,
        "proposals_per_layer": 2,
        "expected_ground_truth_combinations": 4096,
        "controlled_variable": "demand_to_capacity_ratio",
        "nuisance_distribution_policy": "identical_across_gamma_for_each_seed",
        "shared_trunk_utilization_range": [0.75, 0.90],
        "route_access_switch_probability": 0.55,
        "environment_rng_version": "wcnc-final-gamma-environment-v4",
        "truth_evaluator": "independent-demand-capacity-truth-v1",
    }
    if phase == "pilot":
        expected_demand.update(
            {
                "retention_policy": (
                    "ordered_prefix_through_highest_mixed_feasibility_ratio"
                ),
                "retention_inputs": "ground_truth_class_only",
                "refinement_step": 0.025,
            }
        )
    for key, expected in expected_demand.items():
        if demand.get(key) != expected:
            raise ValueError(f"{phase} demand_capacity.{key} must be {expected!r}")

    configured_methods = tuple(config.get("methods", ()))
    ratios = tuple(float(value) for value in config["demand_capacity"]["ratios"])
    if phase == "pilot":
        if ratios != PILOT_GAMMA_GRID:
            raise ValueError(f"pilot gamma grid must be {list(PILOT_GAMMA_GRID)}")
        if configured_methods or tuple(methods):
            raise ValueError("pilot is ground-truth-only and forbids comparison methods")
    else:
        if configured_methods != tuple(METHOD_TO_ENGINE):
            raise ValueError("formal config methods must list all four paired methods")
        if len(methods) != len(METHOD_TO_ENGINE) or set(methods) != set(
            METHOD_TO_ENGINE
        ):
            raise ValueError("formal experiment requires all four paired methods")
        if tuple(methods) != configured_methods:
            raise ValueError("formal runtime methods must match configured order")
        if not ratios:
            raise ValueError("formal gamma grid is not frozen from an accepted pilot")
        validate_formal_pilot_binding(config)
    if validate_registered_seeds and tuple(seeds) != expected_seeds:
        raise ValueError(
            f"formal seeds must be exactly {FORMAL_SEEDS[0]}..{FORMAL_SEEDS[-1]}"
            if phase == "formal"
            else "pilot seeds must be exactly 9000..9009"
        )
    actual_output = output_dir or Path(str(experiment.get("output_dir", "")))
    if actual_output.resolve() != expected_output.resolve():
        raise ValueError(f"{phase} output must be {expected_output}")


def select_pilot_ratios(
    classes_by_gamma: Mapping[float, Collection[str]],
) -> tuple[float, ...]:
    """Retain an ordered prefix through the highest mixed-feasibility point.

    The input deliberately contains class labels only, so method outcomes
    cannot influence pilot retention.
    """

    ordered = sorted(float(value) for value in classes_by_gamma)
    mixed: list[float] = []
    for gamma in ordered:
        labels = tuple(str(value) for value in classes_by_gamma[gamma])
        resolvable = labels.count("RESOLVABLE_CONFLICT")
        unresolvable = labels.count("UNRESOLVABLE_CONFLICT")
        if resolvable >= 1 and 1 <= unresolvable <= len(labels) - 1:
            mixed.append(gamma)
    if not mixed:
        raise ValueError("pilot has no mixed feasibility endpoint")
    gamma_max = max(mixed)
    retained = tuple(gamma for gamma in ordered if gamma <= gamma_max)
    labels_before_endpoint = {
        str(label)
        for gamma in retained
        for label in classes_by_gamma[gamma]
    }
    if "NO_CONFLICT" not in labels_before_endpoint:
        raise ValueError("pilot range lacks an earlier normal-operation region")
    if "RESOLVABLE_CONFLICT" not in labels_before_endpoint:
        raise ValueError("pilot range lacks a contention/resolvable region")
    return retained


def sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_execution_source_manifest(
    *,
    require_clean: bool = True,
) -> dict[str, object]:
    source_sha256 = {
        relative: sha256_file(REPOSITORY_ROOT / relative)
        for relative in EXECUTION_CRITICAL_SOURCES
    }
    status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *EXECUTION_CRITICAL_SOURCES,
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    scoped_clean = not status.stdout.strip()
    if require_clean and not scoped_clean:
        raise ValueError(
            "execution-critical source paths are not clean against HEAD"
        )
    git_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    installed = sorted(
        {
            (
                str(distribution.metadata.get("Name", "unknown"))
                .strip()
                .lower()
                .replace("_", "-")
                + "=="
                + str(distribution.version)
            )
            for distribution in importlib.metadata.distributions()
        }
    )
    return {
        "schema_version": "exp2-source-v1",
        "git_commit": git_commit,
        "scoped_clean": scoped_clean,
        "critical_source_paths": list(EXECUTION_CRITICAL_SOURCES),
        "source_sha256": source_sha256,
        "source_tree_sha256": sha256_json(source_sha256),
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "installed_distributions": installed,
        "dependencies_sha256": sha256_json(installed),
    }


def validate_execution_source_manifest(
    manifest: Mapping[str, object],
    *,
    require_clean: bool = True,
) -> None:
    if manifest.get("schema_version") != "exp2-source-v1":
        raise ValueError("unsupported execution source manifest schema")
    if require_clean and manifest.get("scoped_clean") is not True:
        raise ValueError("execution source manifest is not clean")
    current = build_execution_source_manifest(require_clean=False)
    recorded_sources = manifest.get("source_sha256")
    if recorded_sources != current["source_sha256"]:
        raise ValueError("execution source digest does not match current files")
    if manifest.get("source_tree_sha256") != sha256_json(recorded_sources):
        raise ValueError("execution source tree digest is invalid")
    if manifest.get("git_commit") != current["git_commit"]:
        raise ValueError("execution source Git commit does not match")
    if manifest.get("python") != current["python"]:
        raise ValueError("execution Python environment does not match")
    recorded_distributions = manifest.get("installed_distributions")
    if manifest.get("dependencies_sha256") != sha256_json(
        recorded_distributions
    ):
        raise ValueError("execution dependency digest is invalid")
    if recorded_distributions != current["installed_distributions"]:
        raise ValueError("execution dependency environment does not match")


def validate_formal_pilot_binding(config: dict[str, Any]) -> None:
    experiment = config.get("experiment", {})
    selection_path, selection = _read_bound_json(
        experiment,
        path_field="pilot_selection_path",
        digest_field="pilot_selection_sha256",
        label="pilot selection",
    )
    if selection.get("generator_version") != GENERATOR_VERSION:
        raise ValueError("formal pilot selection generator version does not match")
    if selection.get("selection_inputs") != "ground_truth_class_only":
        raise ValueError("formal pilot selection used comparison-method outcomes")
    retained = tuple(float(value) for value in selection.get("retained_ratios", ()))
    configured = tuple(
        float(value) for value in config.get("demand_capacity", {}).get("ratios", ())
    )
    if not retained or retained != configured:
        raise ValueError("formal gamma ratios do not match accepted pilot selection")
    pilot_config_path = Path(str(experiment.get("pilot_config_path", "")))
    if not pilot_config_path.is_file():
        raise ValueError("formal pilot config file is missing")
    if sha256_file(pilot_config_path) != experiment.get("pilot_config_sha256"):
        raise ValueError("formal pilot config SHA-256 does not match")
    pilot_config = yaml.safe_load(pilot_config_path.read_text(encoding="utf-8")) or {}
    if selection.get("configuration_sha256") != sha256_json(pilot_config):
        raise ValueError("pilot selection does not bind the declared pilot config")

    _execution_path, pilot_execution = _read_bound_json(
        experiment,
        path_field="pilot_execution_manifest_path",
        digest_field="pilot_execution_manifest_sha256",
        label="pilot execution manifest",
    )
    if selection.get("execution_manifest") != pilot_execution:
        raise ValueError("pilot execution manifest does not match pilot selection")
    if selection.get("execution_manifest_sha256") != sha256_json(
        pilot_execution
    ):
        raise ValueError("pilot execution manifest canonical digest does not match")
    expected_execution = {
        "phase": "pilot",
        "generator_version": GENERATOR_VERSION,
        "configuration_sha256": sha256_json(pilot_config),
        "methods": [],
        "seeds": list(PILOT_SEEDS),
        "ratios": list(PILOT_GAMMA_GRID),
        "output_dir": str(PILOT_OUTPUT),
    }
    for field, expected in expected_execution.items():
        if pilot_execution.get(field) != expected:
            raise ValueError(
                f"pilot execution manifest field {field} does not match"
            )

    _source_path, pilot_source = _read_bound_json(
        experiment,
        path_field="pilot_source_manifest_path",
        digest_field="pilot_source_manifest_sha256",
        label="pilot source manifest",
    )
    pilot_source_digest = sha256_json(pilot_source)
    if selection.get("source_manifest_sha256") != pilot_source_digest:
        raise ValueError("pilot source manifest does not match pilot selection")
    if pilot_execution.get("source_manifest_sha256") != pilot_source_digest:
        raise ValueError("pilot source manifest does not match execution manifest")
    source_hashes = pilot_source.get("source_sha256")
    if not isinstance(source_hashes, dict):
        raise ValueError("pilot source manifest has no source hashes")
    if pilot_source.get("scoped_clean") is not True:
        raise ValueError("pilot source manifest was not clean")
    if tuple(pilot_source.get("critical_source_paths", ())) != tuple(
        EXECUTION_CRITICAL_SOURCES
    ):
        raise ValueError("pilot source manifest critical paths do not match")
    pilot_tree_digest = sha256_json(source_hashes)
    if pilot_source.get("source_tree_sha256") != pilot_tree_digest:
        raise ValueError("pilot source manifest tree digest is invalid")
    if experiment.get("critical_source_tree_sha256") != pilot_tree_digest:
        raise ValueError("formal critical source tree does not match accepted pilot")

    current_source = build_execution_source_manifest(require_clean=False)
    if current_source.get("source_tree_sha256") != pilot_tree_digest:
        raise ValueError("formal execution source differs from accepted pilot")
    for label, path_field, digest_field, relative_path in (
        (
            "generator source",
            "generator_source_path",
            "generator_source_sha256",
            "src/simulation/demand_capacity_ratio.py",
        ),
        (
            "truth source",
            "truth_source_path",
            "truth_source_sha256",
            "src/simulation/demand_capacity_truth.py",
        ),
    ):
        source_path = Path(str(experiment.get(path_field, "")))
        if not source_path.is_file():
            raise ValueError(f"formal {label} file is missing")
        source_digest = sha256_file(source_path)
        if source_digest != experiment.get(digest_field):
            raise ValueError(f"formal {label} SHA-256 does not match")
        if source_hashes.get(relative_path) != source_digest:
            raise ValueError(f"formal {label} differs from pilot source manifest")


def _read_bound_json(
    fields: Mapping[str, object],
    *,
    path_field: str,
    digest_field: str,
    label: str,
) -> tuple[Path, dict[str, Any]]:
    path = Path(str(fields.get(path_field, "")))
    if not path.is_file():
        raise ValueError(f"formal {label} file is missing")
    if sha256_file(path) != fields.get(digest_field):
        raise ValueError(f"formal {label} SHA-256 does not match")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"formal {label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"formal {label} must be a JSON object")
    return path, value


def oracle_evaluation_sha256(snapshot) -> str:
    return sha256_json(to_jsonable(snapshot.ground_truth.evaluated_combinations))


def build_pilot_selection(
    evidence: Sequence[dict[str, object]],
    *,
    retained_ratios: Sequence[float],
    generator_version: str,
    execution_manifest: dict[str, object],
    execution_manifest_sha256: str,
) -> dict[str, object]:
    counts: dict[tuple[float, str], int] = {}
    for item in evidence:
        key = (float(item["gamma"]), str(item["conflict_class"]))
        counts[key] = counts.get(key, 0) + 1
    class_counts = [
        {
            "gamma": gamma,
            "conflict_class": conflict_class,
            "samples": counts.get((gamma, conflict_class), 0),
        }
        for gamma in sorted({float(item["gamma"]) for item in evidence})
        for conflict_class in sorted(GROUND_TRUTH_CLASSES)
    ]
    retained = tuple(float(value) for value in retained_ratios)
    return {
        "policy": "ordered_prefix_through_highest_mixed_feasibility_ratio",
        "selection_inputs": "ground_truth_class_only",
        "generator_version": generator_version,
        "candidate_evidence": list(evidence),
        "class_counts": class_counts,
        "retained_ratios": list(retained),
        "highest_retained_gamma": retained[-1],
        "execution_manifest": execution_manifest,
        "execution_manifest_sha256": execution_manifest_sha256,
    }


def persist_execution_provenance(
    raw_dir: Path,
    *,
    config: dict[str, Any],
    methods: Sequence[str],
    seeds: Sequence[int],
    ratios: Sequence[float],
    output_dir: Path,
    pilot_evidence: Sequence[dict[str, object]] = (),
    retained_ratios: Sequence[float] | None = None,
    source_manifest: Mapping[str, object] | None = None,
    require_clean_source: bool = False,
) -> tuple[dict[str, object], str]:
    source = dict(
        source_manifest
        or build_execution_source_manifest(
            require_clean=require_clean_source
        )
    )
    validate_execution_source_manifest(
        source,
        require_clean=require_clean_source,
    )
    source_manifest_sha256 = sha256_json(source)
    configuration_sha256 = _configuration_sha256(config)
    phase = str(config["experiment"]["phase"])
    manifest: dict[str, object] = {
        "phase": phase,
        "generator_version": config["experiment"]["generator_version"],
        "configuration_sha256": configuration_sha256,
        "methods": list(methods),
        "seeds": list(seeds),
        "ratios": [float(value) for value in ratios],
        "output_dir": str(output_dir),
        "source_manifest_sha256": source_manifest_sha256,
    }
    manifest_sha256 = sha256_json(manifest)
    raw_dir.mkdir(parents=True, exist_ok=True)
    _write_json(raw_dir / "execution_source_manifest.json", source)
    _write_json(raw_dir / "execution_manifest.json", manifest)
    if phase == "pilot" and pilot_evidence:
        selection = build_pilot_selection(
            pilot_evidence,
            retained_ratios=retained_ratios or ratios,
            generator_version=str(config["experiment"]["generator_version"]),
            execution_manifest=manifest,
            execution_manifest_sha256=manifest_sha256,
        )
        selection["configuration_sha256"] = configuration_sha256
        selection["source_manifest_sha256"] = source_manifest_sha256
        _write_json(raw_dir / "pilot_selection.json", selection)
    return manifest, manifest_sha256


def run_ground_truth_pilot(
    config: dict[str, Any],
    *,
    seeds: Sequence[int],
    output_dir: Path,
    validate_registered_seeds: bool = True,
    require_clean_source: bool = True,
) -> list[dict[str, object]]:
    """Run class-only range selection without constructing method decisions."""

    validate_protocol(
        config,
        (),
        seeds,
        output_dir,
        validate_registered_seeds=validate_registered_seeds,
    )
    _require_fresh_output(output_dir)
    source_manifest = build_execution_source_manifest(
        require_clean=require_clean_source
    )
    task = config["task"]
    scenario_config = ConflictScenarioConfig(
        num_agents=int(task["num_agents"]),
        edge_ratio=float(task["edge_ratio"]),
        num_gateways=int(task["num_gateways"]),
        cross_gateway_edge_ratio=float(task["cross_gateway_edge_ratio"]),
        multi_hop=True,
    )
    generator = DemandCapacityRatioGenerator()
    configured_ratios = tuple(
        float(value) for value in config["demand_capacity"]["ratios"]
    )
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=False)
    configuration_sha256 = _configuration_sha256(config)
    manifest, manifest_sha = persist_execution_provenance(
        raw_dir,
        config=config,
        methods=(),
        seeds=seeds,
        ratios=configured_ratios,
        output_dir=output_dir,
        source_manifest=source_manifest,
        require_clean_source=require_clean_source,
    )
    rows: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    classes_by_gamma: dict[float, list[str]] = {}

    def evaluate_ratios(ratios: Sequence[float]) -> None:
        for gamma in ratios:
            if gamma in classes_by_gamma:
                continue
            classes_by_gamma[gamma] = []
            for seed in seeds:
                snapshot = generator.generate(gamma, int(seed), scenario_config)
                classes_by_gamma[gamma].append(snapshot.conflict_class)
                evidence.append(
                    {
                        "gamma": gamma,
                        "seed": int(seed),
                        "conflict_class": snapshot.conflict_class,
                        "candidate_combinations": (
                            snapshot.ground_truth.candidate_combinations
                        ),
                    }
                )
                rows.append(compact_scenario_row(snapshot))

    evaluate_ratios(configured_ratios)
    try:
        retained = select_pilot_ratios(classes_by_gamma)
    except ValueError as error:
        if "mixed feasibility endpoint" not in str(error):
            raise
        refinement = _pilot_refinement_ratios(
            classes_by_gamma,
            float(config["demand_capacity"]["refinement_step"]),
        )
        if not refinement:
            raise
        evaluate_ratios(refinement)
        retained = select_pilot_ratios(classes_by_gamma)

    evaluated_ratios = tuple(sorted(classes_by_gamma))
    selection = build_pilot_selection(
        evidence,
        retained_ratios=retained,
        generator_version=str(config["experiment"]["generator_version"]),
        execution_manifest=manifest,
        execution_manifest_sha256=manifest_sha,
    )
    selection["configuration_sha256"] = configuration_sha256
    selection["source_manifest_sha256"] = manifest[
        "source_manifest_sha256"
    ]
    selection["evaluated_ratios"] = list(evaluated_ratios)
    _write_json(raw_dir / "pilot_selection.json", selection)
    for row in rows:
        row["configuration_sha256"] = configuration_sha256
        row["execution_manifest_sha256"] = manifest_sha
    write_jsonl(raw_dir / "scenarios.jsonl", rows)
    _write_json(raw_dir / "configuration.json", config)
    _write_json(
        raw_dir / "pilot_class_counts.json",
        {
            str(gamma): {
                label: labels.count(label) for label in sorted(GROUND_TRUTH_CLASSES)
            }
            for gamma, labels in sorted(classes_by_gamma.items())
        },
    )
    return rows


def _pilot_refinement_ratios(
    classes_by_gamma: Mapping[float, Collection[str]],
    step: float,
) -> tuple[float, ...]:
    all_resolvable = []
    all_unresolvable = []
    for gamma, values in classes_by_gamma.items():
        labels = tuple(str(value) for value in values)
        if labels and "RESOLVABLE_CONFLICT" in labels and (
            "UNRESOLVABLE_CONFLICT" not in labels
        ):
            all_resolvable.append(float(gamma))
        if labels and all(value == "UNRESOLVABLE_CONFLICT" for value in labels):
            all_unresolvable.append(float(gamma))
    if not all_resolvable or not all_unresolvable:
        return ()
    lower = max(all_resolvable)
    upper_candidates = [value for value in all_unresolvable if value > lower]
    if not upper_candidates:
        return ()
    upper = min(upper_candidates)
    count = int(round((upper - lower) / step))
    return tuple(
        round(lower + index * step, 10)
        for index in range(1, count)
        if lower + index * step < upper - 1e-12
    )


def _require_fresh_output(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(
            f"experiment output already contains files: {output_dir}"
        )


def compact_scenario_row(snapshot) -> dict[str, Any]:
    """Serialize audit evidence without 4096 evaluated-combination records."""

    truth = snapshot.ground_truth
    oracle_input = {
        "true_state": to_jsonable(snapshot.true_state),
        "proposals": to_jsonable(snapshot.proposals),
    }
    return {
        "seed": snapshot.seed,
        "experiment": snapshot.experiment,
        "scenario": snapshot.scenario,
        "pressure": snapshot.pressure,
        "conflict_class": snapshot.conflict_class,
        "scenario_fingerprint": snapshot.fingerprint,
        "base_fingerprint": snapshot.base.fingerprint,
        "environment_fingerprint": snapshot.metadata["environment_fingerprint"],
        "flow_r_req_mbps": snapshot.metadata["flow_r_req_mbps"],
        "flow_capacities": snapshot.metadata["flow_capacities"],
        "flow_bottlenecks": snapshot.metadata["flow_bottlenecks"],
        "candidate_resources": snapshot.metadata["candidate_resources"],
        "shared_trunk_utilization": snapshot.metadata[
            "shared_trunk_utilization"
        ],
        "shared_trunk_capacity_mbps": snapshot.metadata[
            "shared_trunk_capacity_mbps"
        ],
        "critical_flow_selection_criterion": snapshot.metadata[
            "critical_flow_selection_criterion"
        ],
        "flow_priorities": snapshot.metadata["flow_priorities"],
        "oracle_input": oracle_input,
        "oracle_input_sha256": sha256_json(oracle_input),
        "oracle_evaluation_sha256": oracle_evaluation_sha256(snapshot),
        "proposal_audit": [
            {
                "proposal_id": proposal.proposal_id,
                "layer": proposal.layer,
                "action": proposal.action,
                "affected_edges": sorted(proposal.affected_edges),
                "write_set": sorted(proposal.write_set),
                "parameters": to_jsonable(proposal.parameters),
            }
            for proposal in snapshot.proposals
        ],
        "ground_truth": {
            "conflict_class": snapshot.conflict_class,
            "candidate_combinations": truth.candidate_combinations,
            "feasible_combinations": len(truth.feasible_combinations),
            "ground_truth_conflict": truth.ground_truth_conflict,
            "ground_truth_resolvable": truth.ground_truth_resolvable,
            "independent_proposal_ids": list(truth.independent_proposal_ids),
            "independent_violations": list(truth.independent_violations),
            "best_feasible_combination": list(truth.best_feasible_combination),
        },
    }


async def run_experiment(
    config: dict[str, Any],
    *,
    methods: Sequence[str],
    seeds: Sequence[int],
    output_dir: Path,
    require_clean_source: bool = True,
) -> list[dict[str, object]]:
    protected = {
        Path("results/exp2").resolve(),
        Path("results/exp2_robustness").resolve(),
        Path("results/exp3").resolve(),
        Path("results/exp2_demand_capacity").resolve(),
    }
    resolved_output = output_dir.resolve()
    if any(
        resolved_output == prior or prior in resolved_output.parents
        for prior in protected
    ):
        raise ValueError("demand/capacity experiment cannot overwrite prior results")
    phase = str(config.get("experiment", {}).get("phase", ""))
    if phase == "pilot":
        return run_ground_truth_pilot(
            config,
            seeds=seeds,
            output_dir=output_dir,
            require_clean_source=require_clean_source,
        )
    validate_protocol(config, methods, seeds, output_dir)
    _require_fresh_output(output_dir)
    source_manifest = build_execution_source_manifest(
        require_clean=require_clean_source
    )
    task = config.get("task", {})
    scenario_config = ConflictScenarioConfig(
        num_agents=int(task.get("num_agents", 10)),
        edge_ratio=float(task.get("edge_ratio", 1.5)),
        num_gateways=int(task.get("num_gateways", 4)),
        cross_gateway_edge_ratio=float(task.get("cross_gateway_edge_ratio", 0.5)),
        multi_hop=True,
    )
    timeout = float(config.get("simulation", {}).get("coordination_timeout_ms", 1000))
    ratios = tuple(float(value) for value in config["demand_capacity"]["ratios"])
    generator = DemandCapacityRatioGenerator()
    configuration_sha256 = _configuration_sha256(config)

    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=False)
    _execution_manifest, execution_manifest_sha256 = persist_execution_provenance(
        raw_dir,
        config=config,
        methods=methods,
        seeds=seeds,
        ratios=ratios,
        output_dir=output_dir,
        source_manifest=source_manifest,
        require_clean_source=require_clean_source,
    )
    rows: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    scenarios: list[dict[str, object]] = []
    run_index = 0
    completed_scenarios = 0
    total_scenarios = len(ratios) * len(seeds)
    for ratio_index, gamma in enumerate(ratios):
        for seed in seeds:
            snapshot = generator.generate(gamma, seed, scenario_config)
            scenario_row = compact_scenario_row(snapshot)
            scenario_row["configuration_sha256"] = configuration_sha256
            scenario_row["execution_manifest_sha256"] = execution_manifest_sha256
            scenarios.append(scenario_row)
            method_offset = (int(seed) + ratio_index) % len(methods)
            ordered_methods = tuple(methods[method_offset:]) + tuple(
                methods[:method_offset]
            )
            for method_order, method in enumerate(ordered_methods):
                if method not in METHOD_TO_ENGINE:
                    raise ValueError(f"unknown demand/capacity method: {method}")
                run_index += 1
                engine_method = METHOD_TO_ENGINE[method]
                run_id = f"gamma={gamma:g}:seed={seed}:method={method}"
                metric, run_events, decision = await run_case(
                    snapshot,
                    engine_method,
                    run_index=run_index,
                    coordination_timeout_ms=timeout,
                    post_evaluator=evaluate_truth,
                )
                metric = replace(
                    metric,
                    run_id=run_id,
                    method=method,
                    result_mode=(
                        "in_memory_transactional_control_plane_gamma_simulation"
                    ),
                )
                for event in run_events:
                    event["experiment_run_id"] = run_id
                    event["method"] = method
                decision["run_id"] = run_id
                decision["method"] = method
                decision["method_order"] = method_order
                environment = snapshot.metadata
                rows.append(
                    {
                        "demand_to_capacity_ratio": gamma,
                        "flow_r_req_mbps_json": json.dumps(
                            environment["flow_r_req_mbps"], sort_keys=True
                        ),
                        "flow_capacities_json": json.dumps(
                            environment["flow_capacities"], sort_keys=True
                        ),
                        "flow_bottlenecks_json": json.dumps(
                            environment["flow_bottlenecks"], sort_keys=True
                        ),
                        "flow_priorities_json": json.dumps(
                            environment["flow_priorities"], sort_keys=True
                        ),
                        "critical_flow_selection_criterion": environment[
                            "critical_flow_selection_criterion"
                        ],
                        "environment_fingerprint": environment[
                            "environment_fingerprint"
                        ],
                        "shared_trunk_utilization": environment[
                            "shared_trunk_utilization"
                        ],
                        "shared_trunk_capacity_mbps": environment[
                            "shared_trunk_capacity_mbps"
                        ],
                        "method_order": method_order,
                        "ground_truth_candidate_combinations": (
                            snapshot.ground_truth.candidate_combinations
                        ),
                        "configuration_sha256": configuration_sha256,
                        "execution_manifest_sha256": execution_manifest_sha256,
                        "oracle_input_sha256": scenario_row["oracle_input_sha256"],
                        "oracle_evaluation_sha256": scenario_row[
                            "oracle_evaluation_sha256"
                        ],
                        **asdict(metric),
                    }
                )
                events.extend(run_events)
                decisions.append(decision)
            completed_scenarios += 1
            if completed_scenarios % 25 == 0 or completed_scenarios == total_scenarios:
                print(
                    f"Exp2 formal progress: {completed_scenarios}/{total_scenarios} "
                    "paired scenarios",
                    flush=True,
                )
    _write_csv(raw_dir / "runs.csv", rows)
    write_jsonl(raw_dir / "events.jsonl", events)
    write_jsonl(raw_dir / "decisions.jsonl", decisions)
    unique = {row["scenario_fingerprint"]: row for row in scenarios}
    write_jsonl(raw_dir / "scenarios.jsonl", unique.values())
    (raw_dir / "configuration.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return rows


def _configuration_sha256(config: dict[str, Any]) -> str:
    return sha256_json(config)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: Sequence[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not rows:
            return
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _parse_seeds(value: str) -> tuple[int, ...]:
    if ":" in value:
        start, finish = (int(item) for item in value.split(":", 1))
        return tuple(range(start, finish + 1))
    return tuple(int(item) for item in value.split(",") if item.strip())


def _parser(config: dict[str, Any] | None = None) -> argparse.ArgumentParser:
    config = config or {}
    simulation = config.get("simulation", {})
    experiment = config.get("experiment", {})
    parser = argparse.ArgumentParser(
        description="WCNC demand-to-capacity cross-layer experiment"
    )
    parser.add_argument(
        "--config", default="configs/exp2_demand_capacity_ratio_v5.yaml"
    )
    parser.add_argument(
        "--methods", default=",".join(config.get("methods", METHOD_TO_ENGINE))
    )
    parser.add_argument("--seeds", default=simulation.get("seed_range", "0:99"))
    parser.add_argument(
        "--output-dir",
        default=experiment.get("output_dir", str(FORMAL_OUTPUT)),
    )
    return parser


def parse_runtime_args(
    argv: Sequence[str] | None = None,
) -> tuple[argparse.Namespace, dict[str, Any]]:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument(
        "--config", default="configs/exp2_demand_capacity_ratio_v5.yaml"
    )
    preliminary, _unknown = config_parser.parse_known_args(argv)
    with Path(preliminary.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    return _parser(config).parse_args(argv), config


def main() -> None:
    args, config = parse_runtime_args()
    methods = tuple(value for value in args.methods.split(",") if value)
    rows = asyncio.run(
        run_experiment(
            config,
            methods=methods,
            seeds=_parse_seeds(args.seeds),
            output_dir=Path(args.output_dir),
        )
    )
    if config.get("experiment", {}).get("phase") == "pilot":
        print(
            f"Exp2 ground-truth pilot completed: {len(rows)} oracle scenarios; "
            "comparison_methods_executed=0"
        )
        return
    non_satisfaction_non_rejection = sum(
        not _bool(row["success"]) and not _bool(row["safe_rejection"])
        for row in rows
    )
    print(
        f"Exp2 demand/capacity completed: {len(rows)} runs; "
        f"non_satisfaction_non_rejection={non_satisfaction_non_rejection}"
    )


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


if __name__ == "__main__":
    main()
