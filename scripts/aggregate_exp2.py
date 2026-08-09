from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean, median, stdev
from typing import Iterable


METRICS = (
    "coordination_latency_ms",
    "feasibility_latency_ms",
    "transaction_latency_ms",
    "total_decision_latency_ms",
    "qos_satisfaction_rate",
    "candidate_combinations",
    "rejected_combinations",
    "rejected_proposals",
    "changed_actions",
    "changed_rules",
    "affected_gateways",
    "control_messages",
    "control_bytes",
    "application_rate_mbps",
    "transport_rate_mbps",
    "network_available_bandwidth_mbps",
    "physical_available_capacity_mbps",
    "latency_ms",
    "packet_loss_rate",
    "reliability",
)


def aggregate_exp2(input_csv: Path, output_dir: Path) -> list[dict[str, object]]:
    with input_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("experiment 2 raw CSV is empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        rows,
        key=lambda row: (
            row["scenario"],
            int(row["seed"]),
            row["method"],
        ),
    )
    _write_rows(output_dir / "by_seed.csv", ordered, list(ordered[0]))
    failures = [row for row in ordered if not _as_bool(row["success"])]
    _write_rows(output_dir / "failures.csv", failures, list(ordered[0]))

    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in ordered:
        groups[(row["scenario"], row["method"])].append(row)
    summary: list[dict[str, object]] = []
    for (scenario, method), group in sorted(groups.items()):
        family, pressure = _scenario_parts(scenario)
        item: dict[str, object] = {
            "scenario": scenario,
            "scenario_family": family,
            "conflict_pressure": pressure,
            "method": method,
            "runs": len(group),
            "successful_runs": sum(_as_bool(row["success"]) for row in group),
            "failed_runs": sum(not _as_bool(row["success"]) for row in group),
            "success_rate": _mean_bool(group, "success"),
            "rollback_rate": _mean_bool(group, "rollback_triggered"),
            "rollback_success_rate": _conditional_rate(
                group,
                "rollback_triggered",
                "rollback_success",
            ),
            "qos_satisfaction_rate": _mean_bool(group, "qos_satisfied"),
            "infeasible_configuration_rate": _mean_bool(
                group,
                "infeasible_configuration",
            ),
            "post_execution_verification_success_rate": _mean_bool(
                group,
                "post_execution_verification_success",
            ),
            "conflict_detection_rate": _conditional_rate(
                group,
                "ground_truth_conflict",
                "conflict_detected",
            ),
            "false_alarm_rate": _negative_condition_rate(
                group,
                "ground_truth_conflict",
                "conflict_detected",
            ),
            "conflict_resolution_rate": _conditional_rate(
                group,
                "ground_truth_resolvable",
                "conflict_resolved",
            ),
            "ground_truth_conflict_rate": _mean_bool(
                group,
                "ground_truth_conflict",
            ),
        }
        for rate in (
            "success_rate",
            "rollback_rate",
            "qos_satisfaction_rate",
            "infeasible_configuration_rate",
            "conflict_detection_rate",
            "false_alarm_rate",
            "conflict_resolution_rate",
        ):
            item[f"{rate}_ci95"] = _rate_ci95(float(item[rate]), len(group))
        for metric in METRICS:
            values = [
                float(row[metric])
                for row in group
                if row.get(metric, "") != ""
            ]
            for name, value in summarize(values).items():
                item[f"{metric}_{name}"] = value
        summary.append(item)
    _write_rows(output_dir / "summary.csv", summary, list(summary[0]))
    pairwise = pairwise_comparison(ordered)
    _write_rows(
        output_dir / "pairwise_comparison.csv",
        pairwise,
        list(pairwise[0]) if pairwise else _pairwise_fields(),
    )
    return summary


def pairwise_comparison(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, int], dict[str, dict[str, str]]] = defaultdict(dict)
    for row in rows:
        if row["scenario"] == "ablation":
            continue
        grouped[(row["scenario"], int(row["seed"]))][row["method"]] = row
    output = []
    for (scenario, seed), methods in sorted(grouped.items()):
        proposed = methods.get("proposed")
        if proposed is None:
            continue
        for method in ("independent", "adjacent", "no_verification"):
            other = methods.get(method)
            if other is None:
                continue
            output.append(
                {
                    "scenario": scenario,
                    "seed": seed,
                    "comparison_method": method,
                    "scenario_fingerprint": proposed["scenario_fingerprint"],
                    "proposed_minus_method_total_decision_latency_ms": (
                        float(proposed["total_decision_latency_ms"])
                        - float(other["total_decision_latency_ms"])
                    ),
                    "proposed_minus_method_coordination_latency_ms": (
                        float(proposed["coordination_latency_ms"])
                        - float(other["coordination_latency_ms"])
                    ),
                    "proposed_minus_method_qos_satisfied": (
                        int(_as_bool(proposed["qos_satisfied"]))
                        - int(_as_bool(other["qos_satisfied"]))
                    ),
                    "proposed_minus_method_infeasible": (
                        int(_as_bool(proposed["infeasible_configuration"]))
                        - int(_as_bool(other["infeasible_configuration"]))
                    ),
                    "proposed_minus_method_changed_rules": (
                        int(float(proposed["changed_rules"]))
                        - int(float(other["changed_rules"]))
                    ),
                    "proposed_minus_method_control_bytes": (
                        int(float(proposed["control_bytes"]))
                        - int(float(other["control_bytes"]))
                    ),
                }
            )
    return output


def summarize(values: Iterable[float]) -> dict[str, float | int | str]:
    materialized = list(values)
    if not materialized:
        return {
            "count": 0,
            "mean": "",
            "median": "",
            "std": "",
            "ci95": "",
            "p95": "",
        }
    deviation = stdev(materialized) if len(materialized) > 1 else 0.0
    return {
        "count": len(materialized),
        "mean": fmean(materialized),
        "median": median(materialized),
        "std": deviation,
        "ci95": confidence_interval_95(materialized),
        "p95": percentile(materialized, 95.0),
    }


def confidence_interval_95(values: Iterable[float]) -> float:
    materialized = list(values)
    if len(materialized) <= 1:
        return 0.0
    return 1.96 * stdev(materialized) / math.sqrt(len(materialized))


def percentile(values: Iterable[float], percentile_value: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile values must not be empty")
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile_value / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _scenario_parts(scenario: str) -> tuple[str, str]:
    if ":pressure=" not in scenario:
        return scenario, ""
    return tuple(scenario.split(":pressure=", 1))  # type: ignore[return-value]


def _mean_bool(rows: list[dict[str, str]], field: str) -> float:
    return sum(_as_bool(row[field]) for row in rows) / len(rows)


def _conditional_rate(
    rows: list[dict[str, str]],
    condition: str,
    outcome: str,
) -> float:
    selected = [row for row in rows if _as_bool(row[condition])]
    if not selected:
        return 0.0
    return sum(_as_bool(row[outcome]) for row in selected) / len(selected)


def _negative_condition_rate(
    rows: list[dict[str, str]],
    condition: str,
    outcome: str,
) -> float:
    selected = [row for row in rows if not _as_bool(row[condition])]
    if not selected:
        return 0.0
    return sum(_as_bool(row[outcome]) for row in selected) / len(selected)


def _rate_ci95(rate: float, count: int) -> float:
    if count <= 0:
        return 0.0
    return 1.96 * math.sqrt(max(0.0, rate * (1.0 - rate)) / count)


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _write_rows(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _pairwise_fields() -> list[str]:
    return [
        "scenario",
        "seed",
        "comparison_method",
        "scenario_fingerprint",
        "proposed_minus_method_total_decision_latency_ms",
        "proposed_minus_method_coordination_latency_ms",
        "proposed_minus_method_qos_satisfied",
        "proposed_minus_method_infeasible",
        "proposed_minus_method_changed_rules",
        "proposed_minus_method_control_bytes",
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate experiment 2")
    parser.add_argument("--input", default="results/exp2/raw/runs.csv")
    parser.add_argument("--output-dir", default="results/exp2/processed")
    return parser


def main() -> None:
    args = _parser().parse_args()
    summary = aggregate_exp2(Path(args.input), Path(args.output_dir))
    print(f"Wrote {len(summary)} experiment-2 summary groups")


if __name__ == "__main__":
    main()
