from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean, median, stdev
from typing import Iterable


METRICS = (
    "elastic_latency_ms",
    "failure_handling_ms",
    "scope_latency_ms",
    "plan_latency_ms",
    "compile_delta_latency_ms",
    "stage_latency_ms",
    "verification_latency_ms",
    "activation_latency_ms",
    "rollback_latency_ms",
    "affected_agents",
    "affected_edges",
    "affected_sessions",
    "affected_routes",
    "affected_gateways",
    "affected_physical_resources",
    "changed_rules_ratio",
    "added_rules",
    "updated_rules",
    "deleted_rules",
    "residual_rules",
    "control_messages",
    "control_bytes",
    "unaffected_edges_interrupted",
    "unaffected_interrupted_rate",
    "unaffected_interruption_ms",
    "unaffected_latency_change_ms",
    "unaffected_packet_loss_change",
    "controller_cpu_percent",
    "controller_memory_mb",
)

SUCCESS_ONLY = {
    "elastic_latency_ms",
    "scope_latency_ms",
    "plan_latency_ms",
    "compile_delta_latency_ms",
    "stage_latency_ms",
    "verification_latency_ms",
    "activation_latency_ms",
}

ROLLBACK_ONLY = {
    "failure_handling_ms",
    "rollback_latency_ms",
}


def aggregate_exp3(input_csv: Path, output_dir: Path) -> list[dict[str, object]]:
    with input_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("experiment 3 raw CSV is empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        rows,
        key=lambda row: (row["scenario"], int(row["seed"]), row["method"]),
    )
    _write_rows(output_dir / "by_seed.csv", ordered, list(ordered[0]))
    failures = [row for row in ordered if not _as_bool(row.get("success", ""))]
    _write_rows(output_dir / "failures.csv", failures, list(ordered[0]))

    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in ordered:
        groups[(row["scenario"], row["method"])].append(row)
    summary: list[dict[str, object]] = []
    for (scenario, method), group in sorted(groups.items()):
        family, value = _scenario_parts(scenario)
        successful = [row for row in group if _as_bool(row["success"])]
        item: dict[str, object] = {
            "scenario": scenario,
            "scenario_family": family,
            "scenario_value": value,
            "method": method,
            "runs": len(group),
            "successful_runs": len(successful),
            "failed_runs": len(group) - len(successful),
            "success_rate": len(successful) / len(group),
            "rollback_rate": sum(_as_bool(row["rollback_triggered"]) for row in group)
            / len(group),
            "rollback_success_rate": _conditional_rate(
                group,
                "rollback_triggered",
                "rollback_success",
            ),
            "residual_rule_rate": sum(float(row["residual_rules"]) > 0 for row in group)
            / len(group),
        }
        for metric in METRICS:
            if metric in SUCCESS_ONLY:
                source = successful
            elif metric in ROLLBACK_ONLY:
                source = [row for row in group if _as_bool(row["rollback_triggered"])]
            else:
                source = group
            values = [float(row[metric]) for row in source if row.get(metric, "") != ""]
            stats = summarize(values)
            for name, result in stats.items():
                item[f"{metric}_{name}"] = result
        summary.append(item)
    fieldnames = list(summary[0])
    _write_rows(output_dir / "summary.csv", summary, fieldnames)
    return summary


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


def _conditional_rate(
    rows: list[dict[str, str]],
    condition: str,
    outcome: str,
) -> float:
    selected = [row for row in rows if _as_bool(row[condition])]
    if not selected:
        return 0.0
    return sum(_as_bool(row[outcome]) for row in selected) / len(selected)


def _scenario_parts(scenario: str) -> tuple[str, str]:
    if ":" not in scenario:
        return scenario, ""
    return tuple(scenario.split(":", 1))  # type: ignore[return-value]


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _write_rows(
    path: Path,
    rows: list[dict[str, object]] | list[dict[str, str]],
    fieldnames: list[str],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate experiment 3 raw runs")
    parser.add_argument("--input", default="results/exp3/raw/runs.csv")
    parser.add_argument("--output-dir", default="results/exp3/processed")
    return parser


def main() -> None:
    args = _parser().parse_args()
    rows = aggregate_exp3(Path(args.input), Path(args.output_dir))
    print(f"Wrote {len(rows)} experiment-3 summary groups to {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
