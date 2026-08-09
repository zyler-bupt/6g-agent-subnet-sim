from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean, median, stdev


LATENCY_METRICS = (
    "control_plane_latency_ms",
    "legacy_controller_compute_ms",
    "formation_latency_ms",
    "formation_latency_s",
    "mapping_latency_ms",
    "binding_latency_ms",
    "feasibility_latency_ms",
    "cross_layer_coordination_latency_ms",
    "compile_latency_ms",
    "transaction_planning_latency_ms",
    "compilation_and_planning_latency_ms",
    "installation_latency_ms",
    "activation_latency_ms",
    "verification_latency_ms",
    "verifier_runtime_ms",
    "session_count",
    "rule_count",
    "control_messages",
    "control_bytes",
)


def aggregate_exp1(input_csv: Path, output_dir: Path) -> list[dict[str, object]]:
    with input_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("experiment 1 raw CSV is empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda row: (int(row["num_agents"]), int(row["seed"])))
    _write_rows(output_dir / "by_seed.csv", ordered, list(ordered[0]))
    failures = [row for row in ordered if not _as_bool(row["success"])]
    _write_rows(output_dir / "failures.csv", failures, list(ordered[0]))
    groups: dict[int, list[dict[str, str]]] = defaultdict(list)
    for row in ordered:
        groups[int(row["num_agents"])].append(row)
    summary: list[dict[str, object]] = []
    for num_agents, group in sorted(groups.items()):
        successful = [row for row in group if _as_bool(row["success"])]
        item: dict[str, object] = {
            "num_agents": num_agents,
            "num_edges_mean": fmean(float(row["num_edges"]) for row in group),
            "num_gateways": int(group[0]["num_gateways"]),
            "runs": len(group),
            "successful_runs": len(successful),
            "failed_runs": len(group) - len(successful),
            "success_rate": len(successful) / len(group),
            "result_mode": group[0]["result_mode"],
            "verification_backend": group[0]["verification_backend"],
            "real_ping_verification": _as_bool(group[0]["real_ping_verification"]),
            "real_iperf3_verification": _as_bool(group[0]["real_iperf3_verification"]),
        }
        for metric in LATENCY_METRICS:
            values = [float(row[metric]) for row in successful if row.get(metric, "") != ""]
            for name, value in summarize(values).items():
                item[f"{metric}_{name}"] = value
        summary.append(item)
    _write_rows(output_dir / "summary.csv", summary, list(summary[0]))
    return summary


def summarize(values: list[float]) -> dict[str, float | int | str]:
    if not values:
        return {"count": 0, "mean": "", "median": "", "std": "", "ci95": "", "p95": ""}
    deviation = stdev(values) if len(values) > 1 else 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        p95 = ordered[0]
    else:
        rank = (len(ordered) - 1) * 0.95
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        weight = rank - lower
        p95 = ordered[lower] * (1.0 - weight) + ordered[upper] * weight
    return {
        "count": len(values),
        "mean": fmean(values),
        "median": median(values),
        "std": deviation,
        "ci95": 1.96 * deviation / math.sqrt(len(values)) if len(values) > 1 else 0.0,
        "p95": p95,
    }


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _write_rows(path: Path, rows, fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate initial formation experiment")
    parser.add_argument("--input", default="results/exp1/raw/runs.csv")
    parser.add_argument("--output-dir", default="results/exp1/processed")
    return parser


def main() -> None:
    args = _parser().parse_args()
    rows = aggregate_exp1(Path(args.input), Path(args.output_dir))
    print(f"Wrote {len(rows)} Exp1 summary groups to {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
