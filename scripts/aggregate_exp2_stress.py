from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


METHODS = (
    "ours",
    "alc",
    "layer_wise_independent",
    "no_global_verification",
)


def aggregate(input_path: Path, output_dir: Path, expected_seeds: int = 100) -> dict[str, object]:
    with input_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    groups: dict[tuple[float, str], list[dict[str, str]]] = {}
    paired: dict[tuple[float, int], list[dict[str, str]]] = {}
    for row in rows:
        intensity = float(row["conflict_pressure"])
        groups.setdefault((intensity, row["method"]), []).append(row)
        paired.setdefault((intensity, int(row["seed"])), []).append(row)
    summary = []
    for (intensity, method), group in sorted(groups.items()):
        satisfaction = [float(_bool(row["qos_satisfied"])) for row in group]
        unsafe = [float(_bool(row["infeasible_configuration"])) for row in group]
        summary.append(
            {
                "conflict_intensity": intensity,
                "method": method,
                "samples": len(group),
                **_proportion_stats(satisfaction, "task_satisfaction_rate"),
                **_proportion_stats(unsafe, "unsafe_configuration_rate"),
            }
        )
    unique_scenarios = {
        (float(row["conflict_pressure"]), int(row["seed"]), row["scenario_fingerprint"]): row
        for row in rows
    }
    class_counts: dict[tuple[float, str], int] = {}
    for (intensity, _seed, _fingerprint), row in unique_scenarios.items():
        key = (intensity, row["conflict_class"])
        class_counts[key] = class_counts.get(key, 0) + 1
    class_rows = [
        {"conflict_intensity": key[0], "conflict_class": key[1], "samples": value}
        for key, value in sorted(class_counts.items())
    ]
    pairing_errors = []
    for key, group in sorted(paired.items()):
        methods = {row["method"] for row in group}
        fingerprints = {row["scenario_fingerprint"] for row in group}
        if methods != set(METHODS) or len(fingerprints) != 1:
            pairing_errors.append(
                {
                    "conflict_intensity": key[0],
                    "seed": key[1],
                    "methods": sorted(methods),
                    "fingerprints": sorted(fingerprints),
                }
            )
    intensities = sorted({float(row["conflict_pressure"]) for row in rows})
    count_errors = [
        {"conflict_intensity": intensity, "method": method, "samples": len(groups.get((intensity, method), ())) }
        for intensity in intensities
        for method in METHODS
        if len(groups.get((intensity, method), ())) != expected_seeds
    ]
    paired_selection_rows = [
        group
        for group in paired.values()
        if {row["method"] for row in group}
        >= {"layer_wise_independent", "no_global_verification"}
    ]
    identical_selection_count = sum(
        next(
            row["selected_proposal_ids"]
            for row in group
            if row["method"] == "layer_wise_independent"
        )
        == next(
            row["selected_proposal_ids"]
            for row in group
            if row["method"] == "no_global_verification"
        )
        for group in paired_selection_rows
    )
    integrity = {
        "pass": not pairing_errors and not count_errors,
        "methods": list(METHODS),
        "expected_seeds_per_point": expected_seeds,
        "raw_runs": len(rows),
        "paired_scenario_seed_groups": len(paired),
        "pairing_errors": pairing_errors,
        "count_errors": count_errors,
        "ground_truth_uses_true_state": all(_bool(row["ground_truth_uses_true_state"]) for row in rows),
        "independent_vs_no_verification": {
            "paired_runs": len(paired_selection_rows),
            "identical_selected_proposal_ids": identical_selection_count,
            "same_selection_rate": (
                identical_selection_count / len(paired_selection_rows)
                if paired_selection_rows
                else 0.0
            ),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / "summary.csv", summary)
    _write(output_dir / "ground_truth_class_counts.csv", class_rows)
    with (output_dir / "experiment_integrity_report.json").open("w", encoding="utf-8") as handle:
        json.dump(integrity, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return integrity


def _proportion_stats(values: list[float], prefix: str) -> dict[str, float]:
    mean = statistics.fmean(values) if values else 0.0
    standard_deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        f"{prefix}_mean": mean,
        f"{prefix}_ci95": 1.96 * standard_deviation / math.sqrt(len(values)) if values else 0.0,
    }


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not rows:
            return
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate WCNC cross-layer stress results")
    parser.add_argument("--input", default="results/exp2_stress/raw/runs.csv")
    parser.add_argument("--output-dir", default="results/exp2_stress/processed")
    parser.add_argument("--expected-seeds", type=int, default=100)
    args = parser.parse_args()
    integrity = aggregate(Path(args.input), Path(args.output_dir), args.expected_seeds)
    print("PASS" if integrity["pass"] else "FAIL", json.dumps(integrity, sort_keys=True))


if __name__ == "__main__":
    main()
