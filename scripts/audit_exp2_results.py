from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import fmean

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.aggregate_exp2 import confidence_interval_95, pairwise_comparison


MAIN_METHODS = {"proposed", "independent", "adjacent", "no_verification"}
MAIN_SCENARIOS = {
    "application_capacity",
    "transport_network",
    "network_physical",
}


def audit_exp2_results(results_dir: Path) -> dict[str, object]:
    raw = results_dir / "raw"
    processed = results_dir / "processed"
    paths = {
        "runs": raw / "runs.csv",
        "events": raw / "events.jsonl",
        "proposals": raw / "proposals.jsonl",
        "conflicts": raw / "conflicts.jsonl",
        "scenarios": raw / "scenarios.jsonl",
        "summary": processed / "summary.csv",
        "failures": processed / "failures.csv",
        "pairwise": processed / "pairwise_comparison.csv",
    }
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise FileNotFoundError("missing experiment-2 artifacts: " + ", ".join(missing))
    runs = _read_csv(paths["runs"])
    events = _read_jsonl(paths["events"])
    proposals = _read_jsonl(paths["proposals"])
    conflicts = _read_jsonl(paths["conflicts"])
    scenarios = _read_jsonl(paths["scenarios"])
    summary = _read_csv(paths["summary"])
    failures = _read_csv(paths["failures"])
    pairwise = _read_csv(paths["pairwise"])

    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, details: object = "") -> None:
        checks.append({"name": name, "status": "PASS" if ok else "FAIL", "details": details})

    main = [
        row
        for row in runs
        if _family(row["scenario"]) in MAIN_SCENARIOS
        and row["method"] in MAIN_METHODS
    ]
    points: dict[tuple[str, str], set[int]] = defaultdict(set)
    point_methods: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in main:
        points[(row["scenario"], row["method"])].add(int(row["seed"]))
        point_methods[(row["scenario"], int(row["seed"]))].add(row["method"])
    bad_seed_points = {
        f"{scenario}/{method}": sorted(seeds)
        for (scenario, method), seeds in points.items()
        if seeds != set(range(30))
    }
    check(
        "30 seeds at every main experiment point",
        not bad_seed_points and len(main) == 2160,
        {"main_runs": len(main), "bad_points": bad_seed_points},
    )
    bad_method_points = {
        f"{scenario}/seed={seed}": sorted(methods)
        for (scenario, seed), methods in point_methods.items()
        if methods != MAIN_METHODS
    }
    check(
        "four methods present for each scenario/seed",
        not bad_method_points,
        bad_method_points,
    )

    fingerprints: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in main:
        fingerprints[(row["scenario"], int(row["seed"]))].add(
            row["scenario_fingerprint"]
        )
    bad_fingerprints = {
        f"{key[0]}/seed={key[1]}": sorted(values)
        for key, values in fingerprints.items()
        if len(values) != 1
    }
    check(
        "scenario fingerprints identical across methods",
        not bad_fingerprints,
        bad_fingerprints,
    )

    proposal_hashes: dict[tuple[str, int, str], dict[str, str]] = defaultdict(dict)
    proposal_material: dict[tuple[str, int, str, str], list[str]] = defaultdict(list)
    for row in proposals:
        if row.get("time_step") is not None or row["scenario"] == "ablation":
            continue
        key = (str(row["scenario"]), int(row["seed"]), str(row["scenario_fingerprint"]), str(row["method"]))
        proposal_material[key].append(
            json.dumps(row["proposal"], sort_keys=True, separators=(",", ":"))
        )
    for (scenario, seed, fingerprint, method), values in proposal_material.items():
        proposal_hashes[(scenario, seed, fingerprint)][method] = "|".join(sorted(values))
    bad_proposals = {
        f"{key[0]}/seed={key[1]}": sorted(values)
        for key, by_method in proposal_hashes.items()
        if len((values := set(by_method.values()))) != 1
    }
    check("proposal pools identical across methods", not bad_proposals, bad_proposals)

    ground_truth: dict[tuple[str, int, str], set[str]] = defaultdict(set)
    for row in conflicts:
        if row.get("source") != "ground_truth" or row.get("time_step") is not None:
            continue
        key = (str(row["scenario"]), int(row["seed"]), str(row["scenario_fingerprint"]))
        payload = {
            "conflict": row.get("ground_truth_conflict"),
            "resolvable": row.get("ground_truth_resolvable"),
            "violations": row.get("independent_violations"),
            "best": row.get("best_feasible_combination"),
        }
        ground_truth[key].add(json.dumps(payload, sort_keys=True))
    bad_ground_truth = {
        f"{key[0]}/seed={key[1]}": sorted(values)
        for key, values in ground_truth.items()
        if len(values) != 1
    }
    check("Ground Truth independent from method", not bad_ground_truth, bad_ground_truth)

    negative = []
    latency_fields = (
        "coordination_latency_ms",
        "feasibility_latency_ms",
        "transaction_latency_ms",
        "total_decision_latency_ms",
    )
    for row in runs:
        for field in latency_fields:
            if float(row[field]) < 0.0:
                negative.append(f"{row['run_id']}:{field}")
        if float(row["decision_finished_at"]) < float(row["event_occurred_at"]):
            negative.append(f"{row['run_id']}:timestamp_order")
    event_groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in events:
        event_groups[(str(row["run_id"]), str(row.get("event_id", "")))].append(
            float(row["timestamp"])
        )
    non_monotonic = [
        f"{key[0]}:{key[1]}"
        for key, values in event_groups.items()
        if values != sorted(values)
    ]
    check(
        "no negative latency or reversed timestamps",
        not negative and not non_monotonic,
        {"negative": negative[:20], "non_monotonic": non_monotonic[:20]},
    )

    infeasible_without_rollback = [
        row["run_id"]
        for row in runs
        if _bool(row["infeasible_configuration"])
        and not row["failure_reason"].startswith("runner_error")
        and not _bool(row["rollback_triggered"])
    ]
    bad_rollback = [
        row["run_id"]
        for row in runs
        if _bool(row["rollback_triggered"])
        and not _bool(row["rollback_success"])
    ]
    check(
        "infeasible executions use successful rollback",
        not infeasible_without_rollback and not bad_rollback,
        {
            "missing_rollback": infeasible_without_rollback[:20],
            "failed_rollback": bad_rollback[:20],
        },
    )

    proposed = [row for row in main if row["method"] == "proposed"]
    proposed_errors = [
        row["run_id"]
        for row in proposed
        if _bool(row["conflict_detected"])
        != _bool(row["ground_truth_conflict"])
        or not _bool(row["success"])
    ]
    check(
        "Proposed detects oracle conflicts and commits feasible actions",
        not proposed_errors and len(proposed) == 540,
        {"runs": len(proposed), "errors": proposed_errors[:20]},
    )

    independent_detected = [
        row["run_id"]
        for row in main
        if row["method"] == "independent" and _bool(row["conflict_detected"])
    ]
    no_verification_detected = [
        row["run_id"]
        for row in main
        if row["method"] == "no_verification"
        and _bool(row["conflict_detected"])
    ]
    check(
        "Independent and no-verification skip global detection",
        not independent_detected and not no_verification_detected,
        {
            "independent": independent_detected[:10],
            "no_verification": no_verification_detected[:10],
        },
    )

    expected_failures = {row["run_id"] for row in runs if not _bool(row["success"])}
    actual_failures = {row["run_id"] for row in failures}
    check(
        "all failed raw runs preserved",
        expected_failures == actual_failures,
        {
            "missing": sorted(expected_failures - actual_failures)[:20],
            "extra": sorted(actual_failures - expected_failures)[:20],
        },
    )

    summary_by_key = {(row["scenario"], row["method"]): row for row in summary}
    grouped_runs: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in runs:
        grouped_runs[(row["scenario"], row["method"])].append(row)
    bad_statistics = []
    for key, group in grouped_runs.items():
        item = summary_by_key.get(key)
        if item is None:
            bad_statistics.append(f"missing:{key}")
            continue
        values = [float(row["coordination_latency_ms"]) for row in group]
        if not math.isclose(
            float(item["coordination_latency_ms_mean"]),
            fmean(values),
            rel_tol=1e-9,
            abs_tol=1e-12,
        ) or not math.isclose(
            float(item["coordination_latency_ms_ci95"]),
            confidence_interval_95(values),
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            bad_statistics.append(str(key))
    check(
        "summary means and CI derive from raw samples",
        not bad_statistics,
        bad_statistics[:20],
    )

    expected_pairwise = pairwise_comparison(runs)
    canonical_expected = sorted(
        json.dumps(row, sort_keys=True) for row in expected_pairwise
    )
    canonical_actual = sorted(
        json.dumps(_normalize_csv_row(row), sort_keys=True) for row in pairwise
    )
    check(
        "pairwise comparison uses matched scenario/seed samples",
        len(canonical_expected) == len(canonical_actual)
        and all(
            _numeric_row_equal(json.loads(left), json.loads(right))
            for left, right in zip(canonical_expected, canonical_actual)
        ),
        {"expected": len(canonical_expected), "actual": len(canonical_actual)},
    )

    executor_runs = {
        str(row["run_id"])
        for row in events
        if row.get("component") == "TransactionExecutor"
    }
    logged_runs = {
        row["run_id"]
        for row in runs
        if not row["failure_reason"].startswith("runner_error")
    }
    check(
        "all methods share logged TransactionExecutor path",
        logged_runs <= executor_runs,
        sorted(logged_runs - executor_runs)[:20],
    )

    check(
        "scenario snapshots are method independent",
        len({row["scenario_fingerprint"] for row in scenarios}) == len(scenarios),
        {"snapshots": len(scenarios)},
    )
    passed = all(item["status"] == "PASS" for item in checks)
    report = {
        "status": "PASS" if passed else "FAIL",
        "checks_passed": sum(item["status"] == "PASS" for item in checks),
        "checks_total": len(checks),
        "raw_run_count": len(runs),
        "main_run_count": len(main),
        "checks": checks,
    }
    report_path = processed / "experiment_integrity_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _family(scenario: str) -> str:
    return scenario.split(":pressure=", 1)[0]


def _bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _normalize_csv_row(row: dict[str, str]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in row.items():
        if key in {"scenario", "comparison_method", "scenario_fingerprint"}:
            output[key] = value
        elif key == "seed":
            output[key] = int(value)
        else:
            output[key] = float(value)
    return output


def _numeric_row_equal(left: dict, right: dict) -> bool:
    if left.keys() != right.keys():
        return False
    for key in left:
        if isinstance(left[key], (int, float)) and isinstance(right[key], (int, float)):
            if not math.isclose(float(left[key]), float(right[key]), rel_tol=1e-9, abs_tol=1e-9):
                return False
        elif left[key] != right[key]:
            return False
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit experiment-2 results")
    parser.add_argument("--results-dir", default="results/exp2")
    return parser


def main() -> None:
    args = _parser().parse_args()
    report = audit_exp2_results(Path(args.results_dir))
    print(f"{report['status']} ({report['checks_passed']}/{report['checks_total']})")
    for item in report["checks"]:
        if item["status"] == "FAIL":
            print(f"FAIL: {item['name']}: {item['details']}")
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
