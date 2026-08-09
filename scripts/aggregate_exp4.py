from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean, median, stdev
from typing import Iterable


METRICS = (
    "detection_latency_ms",
    "localization_latency_ms",
    "proposal_latency_ms",
    "coordination_latency_ms",
    "feasibility_latency_ms",
    "delta_compile_latency_ms",
    "stage_latency_ms",
    "activation_latency_ms",
    "verification_latency_ms",
    "reconfiguration_latency_ms",
    "repair_latency_ms",
    "recovery_latency_ms",
    "failure_handling_ms",
    "service_interruption_ms",
    "changed_agents",
    "changed_sessions",
    "changed_routes",
    "changed_physical_bindings",
    "changed_rules",
    "rule_change_ratio",
    "gateway_impact_ratio",
    "affected_gateways",
    "collateral_interruption_rate",
    "collateral_interruption_ms",
    "packet_loss_during_recovery",
    "throughput_before",
    "throughput_during",
    "throughput_after",
    "latency_before",
    "latency_during",
    "latency_after",
    "control_messages",
    "control_bytes",
)

SUCCESS_ONLY = {
    "repair_latency_ms",
    "recovery_latency_ms",
    "service_interruption_ms",
}
ROLLBACK_ONLY = {"failure_handling_ms"}


def aggregate_exp4(input_csv: Path, output_dir: Path) -> list[dict[str, object]]:
    rows = _read_csv(input_csv)
    if not rows:
        raise ValueError("experiment 4 raw CSV is empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda item: (item["scenario"], int(item["seed"]), item["method"]))
    _write_rows(output_dir / "by_seed.csv", rows, list(rows[0]))
    failures = [item for item in rows if not _bool(item["success"])]
    _write_rows(output_dir / "failures.csv", failures, list(rows[0]))

    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[(row["scenario"], row["method"])].append(row)
    summary: list[dict[str, object]] = []
    for (scenario, method), group in sorted(groups.items()):
        successful = [item for item in group if _bool(item["recovery_success"])]
        rollback = [item for item in group if _bool(item["rollback_triggered"])]
        item: dict[str, object] = {
            "scenario": scenario,
            "fault_family": scenario.split(":", 1)[0],
            "fault_level": scenario.split(":", 1)[1] if ":" in scenario else "",
            "method": method,
            "runs": len(group),
            "successful_runs": len(successful),
            "failed_runs": len(group) - len(successful),
            "success_rate": len(successful) / len(group),
            "qos_recovery_rate": sum(_bool(row["qos_recovered"]) for row in group) / len(group),
            "safe_rejection_rate": sum(_bool(row["safe_rejection"]) for row in group) / len(group),
            "rollback_rate": len(rollback) / len(group),
            "rollback_success_rate": (
                sum(_bool(row["rollback_success"]) for row in rollback) / len(rollback)
                if rollback
                else 0.0
            ),
            "residual_rule_rate": sum(float(row["residual_rules"]) > 0 for row in group) / len(group),
        }
        for metric in METRICS:
            source = successful if metric in SUCCESS_ONLY else rollback if metric in ROLLBACK_ONLY else group
            values = [float(row[metric]) for row in source if row.get(metric, "") != ""]
            for name, value in summarize(values).items():
                item[f"{metric}_{name}"] = value
        summary.append(item)
    _write_rows(output_dir / "summary.csv", summary, list(summary[0]))
    pairwise = paired_comparisons(rows)
    _write_rows(
        output_dir / "pairwise_comparison.csv",
        pairwise,
        list(pairwise[0]) if pairwise else ["fault_family"],
    )
    return summary


def summarize(values: Iterable[float]) -> dict[str, float | int | str]:
    data = list(values)
    if not data:
        return {"count": 0, "mean": "", "median": "", "std": "", "ci95": "", "p95": ""}
    deviation = stdev(data) if len(data) > 1 else 0.0
    return {
        "count": len(data),
        "mean": fmean(data),
        "median": median(data),
        "std": deviation,
        "ci95": 1.96 * deviation / math.sqrt(len(data)) if len(data) > 1 else 0.0,
        "p95": percentile(data, 95.0),
    }


def percentile(values: Iterable[float], percentile_value: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile input is empty")
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile_value / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def paired_comparisons(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    indexed = {
        (row["scenario"], int(row["seed"]), row["method"]): row
        for row in rows
    }
    output: list[dict[str, object]] = []
    for family in sorted({row["scenario"].split(":", 1)[0] for row in rows}) + ["ALL"]:
        family_rows = [
            row for row in rows
            if family == "ALL" or row["scenario"].split(":", 1)[0] == family
        ]
        pairs = sorted({(row["scenario"], int(row["seed"])) for row in family_rows})
        for method_b in ("full_rebuild", "network_only"):
            for metric in (
                "recovery_latency_ms",
                "rule_change_ratio",
                "gateway_impact_ratio",
                "service_interruption_ms",
            ):
                differences = []
                for scenario, seed in pairs:
                    left = indexed.get((scenario, seed, "proposed"))
                    right = indexed.get((scenario, seed, method_b))
                    if left is None or right is None:
                        continue
                    if metric in SUCCESS_ONLY and not (
                        _bool(left["recovery_success"]) and _bool(right["recovery_success"])
                    ):
                        continue
                    differences.append(float(left[metric]) - float(right[metric]))
                test = wilcoxon_signed_rank(differences)
                stats = summarize(differences)
                output.append(
                    {
                        "fault_family": family,
                        "metric": metric,
                        "method_a": "proposed",
                        "method_b": method_b,
                        "mean_difference": stats["mean"],
                        "median_difference": stats["median"],
                        "ci95_half_width": stats["ci95"],
                        "paired_test": "wilcoxon_signed_rank_normal_approximation",
                        "p_value": test["p_value"],
                        "effect_size": test["rank_biserial"],
                        "sample_size": len(differences),
                    }
                )
            binary = []
            for scenario, seed in pairs:
                left = indexed.get((scenario, seed, "proposed"))
                right = indexed.get((scenario, seed, method_b))
                if left is not None and right is not None:
                    binary.append((_bool(left["recovery_success"]), _bool(right["recovery_success"])))
            mcnemar = mcnemar_exact(binary)
            output.append(
                {
                    "fault_family": family,
                    "metric": "recovery_success",
                    "method_a": "proposed",
                    "method_b": method_b,
                    "mean_difference": (
                        fmean(float(a) - float(b) for a, b in binary) if binary else ""
                    ),
                    "median_difference": "",
                    "ci95_half_width": "",
                    "paired_test": "mcnemar_exact",
                    "p_value": mcnemar["p_value"],
                    "effect_size": mcnemar["matched_odds_ratio"],
                    "sample_size": len(binary),
                }
            )
    return output


def wilcoxon_signed_rank(differences: Iterable[float]) -> dict[str, float]:
    nonzero = [value for value in differences if abs(value) > 1e-12]
    if not nonzero:
        return {"p_value": 1.0, "rank_biserial": 0.0}
    ordered = sorted(enumerate(nonzero), key=lambda item: abs(item[1]))
    ranks = [0.0] * len(nonzero)
    index = 0
    while index < len(ordered):
        finish = index + 1
        while finish < len(ordered) and abs(ordered[finish][1]) == abs(ordered[index][1]):
            finish += 1
        rank = (index + 1 + finish) / 2.0
        for original, _value in ordered[index:finish]:
            ranks[original] = rank
        index = finish
    positive = sum(rank for rank, value in zip(ranks, nonzero) if value > 0)
    negative = sum(rank for rank, value in zip(ranks, nonzero) if value < 0)
    n = len(nonzero)
    mean = n * (n + 1) / 4.0
    variance = n * (n + 1) * (2 * n + 1) / 24.0
    z = (min(positive, negative) - mean + 0.5) / math.sqrt(max(variance, 1e-12))
    p_value = min(1.0, math.erfc(abs(z) / math.sqrt(2.0)))
    total = positive + negative
    return {
        "p_value": p_value,
        "rank_biserial": (positive - negative) / total if total else 0.0,
    }


def mcnemar_exact(pairs: Iterable[tuple[bool, bool]]) -> dict[str, float]:
    b = sum(left and not right for left, right in pairs)
    c = sum(not left and right for left, right in pairs)
    discordant = b + c
    if discordant == 0:
        return {"p_value": 1.0, "matched_odds_ratio": 1.0}
    smaller = min(b, c)
    p_value = min(
        1.0,
        2.0 * sum(math.comb(discordant, index) for index in range(smaller + 1)) / (2**discordant),
    )
    odds = (b + 0.5) / (c + 0.5)
    return {"p_value": p_value, "matched_odds_ratio": odds}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _write_rows(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate experiment 4 recovery results")
    parser.add_argument("--input", default="results/exp4/raw/runs.csv")
    parser.add_argument("--output-dir", default="results/exp4/processed")
    return parser


def main() -> None:
    args = _parser().parse_args()
    summary = aggregate_exp4(Path(args.input), Path(args.output_dir))
    print(f"Wrote {len(summary)} Exp4 summary groups to {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
