from __future__ import annotations

import argparse
import csv
import math
import statistics
from pathlib import Path


def aggregate(input_path: Path, output_dir: Path) -> list[dict[str, object]]:
    with input_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    groups: dict[int, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(int(row["num_agents"]), []).append(row)
    summary: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for num_agents, group in sorted(groups.items()):
        successful = [row for row in group if _bool(row["success"])]
        failures.extend(row for row in group if not _bool(row["success"]))
        values = [float(row["verified_formation_latency_s"]) for row in successful]
        retry_runs = [row for row in group if int(row["retry_count"]) > 0]
        ping_retry_runs = [row for row in group if int(row["ping_retried_edges"]) > 0]
        iperf_retry_runs = [row for row in group if int(row["iperf_retried_flows"]) > 0]
        background_runs = [row for row in group if _bool(row["background_traffic_enabled"])]
        blocks = {int(row["block_index"]) for row in group if row.get("block_index")}
        order_positions = {
            int(row["order_position"]) for row in group if row.get("order_position")
        }
        run_sequences = [
            int(row["run_sequence"]) for row in group if row.get("run_sequence")
        ]
        summary.append(
            {
                "num_agents": num_agents,
                "runs_total": len(group),
                "runs_successful": len(successful),
                "runs_failed": len(group) - len(successful),
                "success_rate": len(successful) / len(group) if group else 0.0,
                "runs_with_retry": len(retry_runs),
                "retry_rate": len(retry_runs) / len(group) if group else 0.0,
                "runs_with_ping_retry": len(ping_retry_runs),
                "ping_retry_rate": len(ping_retry_runs) / len(group) if group else 0.0,
                "runs_with_iperf3_retry": len(iperf_retry_runs),
                "iperf3_retry_rate": len(iperf_retry_runs) / len(group) if group else 0.0,
                "background_traffic_runs": len(background_runs),
                "background_traffic_rate": len(background_runs) / len(group) if group else 0.0,
                "blocks_observed": len(blocks),
                "order_positions_observed": ",".join(
                    str(value) for value in sorted(order_positions)
                ),
                "run_sequence_min": min(run_sequences) if run_sequences else 0,
                "run_sequence_max": max(run_sequences) if run_sequences else 0,
                **_stats(values, "verified_formation_latency_s"),
            }
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / "summary.csv", summary)
    _write(output_dir / "failures.csv", failures, fallback_fields=list(rows[0]) if rows else ())
    return summary


def _stats(values: list[float], prefix: str) -> dict[str, float | int]:
    if not values:
        return {
            "samples": 0,
            f"{prefix}_mean": 0.0,
            f"{prefix}_median": 0.0,
            f"{prefix}_std": 0.0,
            f"{prefix}_ci95": 0.0,
            f"{prefix}_p95": 0.0,
        }
    standard_deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "samples": len(values),
        f"{prefix}_mean": statistics.fmean(values),
        f"{prefix}_median": statistics.median(values),
        f"{prefix}_std": standard_deviation,
        f"{prefix}_ci95": 1.96 * standard_deviation / math.sqrt(len(values)),
        f"{prefix}_p95": ordered[p95_index],
    }


def _write(path: Path, rows: list[dict], fallback_fields=()) -> None:
    fields = list(rows[0]) if rows else list(fallback_fields)
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate real netns verified formation runs")
    parser.add_argument("--input", default="results/exp1_wcnc_final_v2/raw/runs.csv")
    parser.add_argument("--output-dir", default="results/exp1_wcnc_final_v2/processed")
    args = parser.parse_args()
    rows = aggregate(Path(args.input), Path(args.output_dir))
    print(f"Aggregated {len(rows)} Exp1 netns task-size points")


if __name__ == "__main__":
    main()
