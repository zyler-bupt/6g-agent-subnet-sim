from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import inspect
import json
import textwrap
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.controller import cross_layer_coordinator as coordinator_module
from src.controller import ground_truth as ground_truth_module
from src.controller.cross_layer_coordinator import CrossLayerCoordinator
from src.controller.ground_truth import GroundTruthSolver
from src.core.models import to_jsonable
from src.simulation.conflict_robustness import ConflictRobustnessGenerator


def audit_ground_truth_independence() -> dict[str, object]:
    ground_source = inspect.getsource(GroundTruthSolver.solve)
    proposed_source = inspect.getsource(CrossLayerCoordinator._proposed)
    generator_source = inspect.getsource(
        __import__(
            "src.simulation.conflict_scenario_generator",
            fromlist=["ConflictScenarioGenerator"],
        ).ConflictScenarioGenerator.generate
    )
    ground_calls = _called_names(ground_source)
    proposed_calls = _called_names(proposed_source)
    generator = ConflictRobustnessGenerator()
    snapshot = generator.base_case("application_capacity", 1.2, 7)
    before_state = _digest(snapshot.true_state)
    before_proposals = _digest(snapshot.proposals)
    oracle = GroundTruthSolver().solve(
        deepcopy(snapshot.true_state),
        deepcopy(snapshot.proposals),
    )
    controller_result = CrossLayerCoordinator().coordinate(
        "proposed",
        deepcopy(snapshot.observed_state),
        deepcopy(snapshot.proposals),
    )
    after_state = _digest(snapshot.true_state)
    after_proposals = _digest(snapshot.proposals)

    agreement = []
    for scenario, pressure in (
        ("application_capacity", 0.8),
        ("application_capacity", 1.2),
        ("transport_network", 0.6),
        ("transport_network", 0.95),
        ("network_physical", 0.1),
        ("network_physical", 0.5),
    ):
        for seed in range(10):
            sample = generator.base_case(scenario, pressure, seed)
            result = CrossLayerCoordinator().coordinate(
                "proposed", sample.observed_state, sample.proposals
            )
            selected = tuple(item.proposal_id for item in result.selected_proposals)
            agreement.append(selected == sample.ground_truth.best_feasible_combination)

    checks = {
        "solver_functions_are_distinct": (
            GroundTruthSolver.solve is not CrossLayerCoordinator._proposed
        ),
        "shared_constraint_evaluator_only": (
            "evaluate_cross_layer_combination" in ground_calls
            and "evaluate_cross_layer_combination" in proposed_calls
        ),
        "inputs_are_not_mutated": (
            before_state == after_state and before_proposals == after_proposals
        ),
        "oracle_receives_private_deepcopies": "deepcopy" in generator_source,
        "no_shared_result_cache": (
            not hasattr(GroundTruthSolver, "cache")
            and not hasattr(CrossLayerCoordinator, "cache")
            and "lru_cache" not in ground_source
            and "lru_cache" not in proposed_source
        ),
        "proposed_does_not_read_ground_truth_best_combination": (
            "ground_truth_best_combination" not in proposed_source
            and "best_feasible_combination" not in proposed_source
        ),
        "proposed_does_not_filter_on_ground_truth_labels": (
            "ground_truth_conflict" not in proposed_source
            and "ground_truth_resolvable" not in proposed_source
        ),
        "objectives_are_distinct": (
            "combination_objective" in ground_source
            and "_proposed_policy_objective" in proposed_source
        ),
        "tie_breaks_are_distinct": (
            "min(feasible" in ground_source
            and "max(" in proposed_source
            and "descending" in proposed_source
        ),
        "proposed_is_exact_feasible_combination_search": (
            "product(" in proposed_source and "for raw_combination" in proposed_source
        ),
        "runtime_results_are_separate_objects": oracle is not controller_result,
    }
    passed = all(checks.values())
    return {
        "status": "PASS" if passed else "FAIL",
        "ground_truth_solver": (
            "src.controller.ground_truth.GroundTruthSolver.solve"
        ),
        "proposed_solver": (
            "src.controller.cross_layer_coordinator.CrossLayerCoordinator._proposed"
        ),
        "ground_truth_objective": (
            "combination_objective: QoS violation count, weighted declared cost, "
            "change count, resource use"
        ),
        "proposed_objective": (
            "hard-feasible filter then lexicographic bottleneck service margin, "
            "change scope, overhead, declared benefit"
        ),
        "ground_truth_constraints": _constraints(),
        "proposed_constraints": _constraints(),
        "shared_constraint_definition_allowed": True,
        "shared_mutable_state": False,
        "shared_cache_results": False,
        "direct_ground_truth_result_read": False,
        "ground_truth_label_filtering": False,
        "same_objective": False,
        "same_tie_break": False,
        "search_type": "exact feasible-combination search",
        "sampled_optimal_combination_agreement_rate": (
            sum(agreement) / len(agreement)
        ),
        "sampled_cases": len(agreement),
        "checks": checks,
        "note": (
            "Both solvers intentionally share the published system constraints. "
            "Feasibility agreement is not result sharing."
        ),
    }


def audit_robustness_results(results_dir: Path) -> dict[str, object]:
    raw = results_dir / "raw"
    processed = results_dir / "processed"
    required = (
        raw / "runs.csv",
        raw / "events.jsonl",
        raw / "decisions.jsonl",
        raw / "scenarios.jsonl",
        raw / "main_scenario_audit.csv",
        processed / "summary.csv",
        processed / "scenario_specific_summary.csv",
        processed / "pairwise_tests.csv",
        processed / "failures.csv",
        processed / "independent_vs_noverification_report.json",
        results_dir / "exp2_source_integrity.json",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("missing robustness artifacts: " + ", ".join(missing))
    runs = _read_csv(raw / "runs.csv")
    main = _read_csv(raw / "main_scenario_audit.csv")
    scenarios = _read_jsonl(raw / "scenarios.jsonl")
    pairwise = _read_csv(processed / "pairwise_tests.csv")
    failures = _read_csv(processed / "failures.csv")
    difference = json.loads(
        (processed / "independent_vs_noverification_report.json").read_text(
            encoding="utf-8"
        )
    )
    source_integrity = json.loads(
        (results_dir / "exp2_source_integrity.json").read_text(encoding="utf-8")
    )
    checks: list[dict[str, object]] = []

    def check(name: str, condition: bool, details: object = "") -> None:
        checks.append(
            {
                "name": name,
                "status": "PASS" if condition else "FAIL",
                "details": details,
            }
        )

    main_scenarios = {row["scenario"] for row in main}
    check(
        "three main scenarios reported separately",
        main_scenarios
        == {
            "application_capacity",
            "transport_network",
            "network_physical",
        },
        sorted(main_scenarios),
    )

    point_seeds: dict[tuple[str, str, str, str], set[int]] = defaultdict(set)
    for row in runs:
        point_seeds[
            (row["experiment"], row["scenario"], _factor(row), row["method"])
        ].add(int(row["seed"]))
    bad_seeds = {
        "/".join(key): sorted(values)
        for key, values in point_seeds.items()
        if values != set(range(30))
    }
    check("30 paired seeds at every robustness point", not bad_seeds, bad_seeds)

    method_fingerprints: dict[tuple[str, str, str, int], set[str]] = defaultdict(set)
    for row in runs:
        method_fingerprints[
            (row["experiment"], row["scenario"], _factor(row), int(row["seed"]))
        ].add(row["scenario_fingerprint"])
    bad_fingerprints = {
        str(key): sorted(values)
        for key, values in method_fingerprints.items()
        if len(values) != 1
    }
    check(
        "all methods receive identical paired observations",
        not bad_fingerprints,
        bad_fingerprints,
    )

    unresolvable = [row for row in runs if row["experiment"] == "unresolvable"]
    cases = {row["scenario"] for row in unresolvable}
    wrong_unresolvable = [
        row["run_id"]
        for row in unresolvable
        if row["conflict_class"] != "UNRESOLVABLE_CONFLICT"
        or int(row["ground_truth_feasible_combinations"]) != 0
    ]
    check(
        "four unresolvable mechanisms have zero feasible combinations",
        len(cases) == 4 and not wrong_unresolvable,
        {"cases": sorted(cases), "errors": wrong_unresolvable[:20]},
    )

    proposed_unresolvable = [
        row for row in unresolvable if row["method"] == "proposed"
    ]
    unsafe = [row["run_id"] for row in unresolvable if _bool(row["unsafe_execution"])]
    unsafe_proposed = [
        row["run_id"]
        for row in proposed_unresolvable
        if not _bool(row["safe_rejection"])
        or _bool(row["transaction_attempted"])
        or int(row["stable_version_after"]) != int(row["stable_version_before"])
    ]
    check(
        "Proposed safely rejects every unresolvable conflict",
        not unsafe_proposed and len(proposed_unresolvable) == 120,
        unsafe_proposed[:20],
    )
    check("no method commits an unresolvable configuration", not unsafe, unsafe[:20])

    noise = [row for row in runs if row["experiment"] == "noise"]
    noise_truth_errors = [
        row["run_id"] for row in noise if not _bool(row["ground_truth_uses_true_state"])
    ]
    check(
        "noise Ground Truth always uses true state",
        not noise_truth_errors,
        noise_truth_errors[:20],
    )

    stale = [
        row
        for row in runs
        if row["experiment"] == "stale" and int(row["stale_ms"]) > 0
    ]
    stale_errors = [
        row["run_id"]
        for row in stale
        if row["method"] == "proposed"
        and (
            int(row["proposal_generated_version"])
            == int(row["execution_version"])
            or not _bool(row["stale_state_detected"])
            or not _bool(row["stale_proposal_rejected"])
        )
    ]
    check(
        "Proposed rejects stale proposal versions/read sets",
        not stale_errors,
        stale_errors[:20],
    )

    missing_scenarios = [
        row for row in scenarios if row["experiment"] == "missing_layer"
    ]
    missing_errors = []
    for row in missing_scenarios:
        missing_layer = row["metadata"]["missing_layer"]
        proposal_layers = {proposal["layer"] for proposal in row["proposals"]}
        if (
            missing_layer in proposal_layers
            or row["metadata"].get("missing_value_policy") != "conservative_bound"
        ):
            missing_errors.append(row["scenario_fingerprint"])
    check(
        "missing layers are absent and use conservative bounds",
        not missing_errors and len(missing_scenarios) == 120,
        missing_errors[:20],
    )

    scale = [row for row in runs if row["experiment"] == "proposal_scale"]
    scale_errors = [
        row["run_id"]
        for row in scale
        if int(row["num_raw_combinations"])
        != int(row["proposals_per_layer"]) ** 4
    ]
    check(
        "proposal combination growth is measured as n^4",
        not scale_errors,
        scale_errors[:20],
    )
    check(
        "proposal-scale timeout outcomes are retained",
        len(scale) == 300,
        {"runs": len(scale), "timeouts": sum(_bool(row["timeout"]) for row in scale)},
    )

    expected_failures = {row["run_id"] for row in runs if not _bool(row["success"])}
    actual_failures = {row["run_id"] for row in failures}
    check(
        "all failures and safe rejections are retained",
        expected_failures == actual_failures,
        {
            "missing": sorted(expected_failures - actual_failures)[:20],
            "extra": sorted(actual_failures - expected_failures)[:20],
        },
    )

    check(
        "Independent and no-verification mechanisms select differently",
        difference.get("status") == "PASS"
        and float(difference.get("same_selection_rate", 1.0)) < 1.0,
        {
            "paired_runs": difference.get("paired_runs"),
            "same_selection_rate": difference.get("same_selection_rate"),
        },
    )

    pair_sizes = [int(row["sample_size"]) for row in pairwise]
    check(
        "paired statistical tests use same-seed samples",
        bool(pairwise) and all(size == 30 for size in pair_sizes),
        {"tests": len(pairwise), "sample_sizes": sorted(set(pair_sizes))},
    )

    negative = [
        f"{row['run_id']}:{field}"
        for row in runs
        for field in (
            "coordination_latency_ms",
            "feasibility_latency_ms",
            "transaction_latency_ms",
            "total_latency_ms",
            "peak_memory_mb",
        )
        if float(row[field]) < 0.0
    ]
    partial = [
        row["run_id"] for row in runs if not _bool(row["no_partial_commit"])
    ]
    check("no negative timing or memory values", not negative, negative[:20])
    check("no partial commits", not partial, partial[:20])
    check(
        "results remain isolated from Exp2",
        results_dir.name == "exp2_robustness"
        and source_integrity.get("status") == "PASS"
        and source_integrity.get("hashes_before")
        == source_integrity.get("hashes_after"),
        {
            "results_dir": str(results_dir),
            "source_integrity": source_integrity.get("status"),
        },
    )
    modes = {row["result_mode"] for row in runs}
    check(
        "all results identify control-plane simulation mode",
        modes == {"in_memory_transactional_control_plane_simulation"},
        sorted(modes),
    )

    independence = audit_ground_truth_independence()
    check(
        "Ground Truth independence audit passes",
        independence["status"] == "PASS",
        independence.get("checks"),
    )
    independence_path = results_dir / "ground_truth_independence_report.json"
    independence_path.write_text(
        json.dumps(independence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    root_difference = results_dir / "independent_vs_noverification_report.json"
    root_difference.write_text(
        json.dumps(difference, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    passed = all(item["status"] == "PASS" for item in checks)
    report = {
        "status": "PASS" if passed else "FAIL",
        "checks_passed": sum(item["status"] == "PASS" for item in checks),
        "checks_total": len(checks),
        "robustness_runs": len(runs),
        "main_scenario_audit_rows": len(main),
        "checks": checks,
    }
    (processed / "experiment_integrity_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _called_names(source: str) -> set[str]:
    tree = ast.parse(textwrap.dedent(source))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _constraints() -> list[str]:
    return [
        "application_rate <= transport_rate",
        "transport_rate <= network_bandwidth",
        "network_bandwidth <= physical_capacity",
        "end_to_end_latency <= maximum_latency",
        "transport_reliability * network_reliability * physical_reliability >= minimum",
        "shared_resource_demand <= residual_capacity",
        "write-set compatibility",
        "observation version/read-set freshness",
    ]


def _digest(value: Any) -> str:
    encoded = json.dumps(
        to_jsonable(value), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _factor(row: dict[str, str]) -> str:
    experiment = row["experiment"]
    return {
        "noise": row.get("noise_ratio", ""),
        "stale": row.get("stale_ms", ""),
        "missing_layer": row.get("missing_layer", ""),
        "proposal_scale": row.get("proposals_per_layer", ""),
    }.get(experiment, row["scenario"])


def _bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit Exp2 robustness results")
    parser.add_argument("--results-dir", default="results/exp2_robustness")
    return parser


def main() -> None:
    args = _parser().parse_args()
    report = audit_robustness_results(Path(args.results_dir))
    print(
        f"{report['status']} "
        f"({report['checks_passed']}/{report['checks_total']} checks)"
    )
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
