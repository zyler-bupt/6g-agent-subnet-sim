from __future__ import annotations

import argparse
import csv
import hashlib
import math
import random
from collections import defaultdict
from pathlib import Path


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def truth(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def wilson(numerator: int, denominator: int) -> tuple[float, float, float]:
    if denominator == 0:
        return math.nan, math.nan, math.nan
    z = 1.959963984540054
    p = numerator / denominator
    scale = 1 + z * z / denominator
    center = (p + z * z / (2 * denominator)) / scale
    half = z * math.sqrt(p * (1 - p) / denominator + z * z / (4 * denominator**2)) / scale
    return p, max(0.0, center - half), min(1.0, center + half)


def cluster_bootstrap(values: list[tuple[int, float]], iterations: int = 5000) -> tuple[float, float, float]:
    if not values:
        return math.nan, math.nan, math.nan
    clusters: dict[int, list[float]] = defaultdict(list)
    for seed, value in values:
        clusters[seed].append(value)
    keys = sorted(clusters)
    mean = sum(value for _, value in values) / len(values)
    rng = random.Random("wcnc_final_v3_cluster_bootstrap")
    samples = []
    for _ in range(iterations):
        selected = [rng.choice(keys) for _ in keys]
        materialized = [value for key in selected for value in clusters[key]]
        samples.append(sum(materialized) / len(materialized))
    samples.sort()
    return mean, samples[int(0.025 * (iterations - 1))], samples[int(0.975 * (iterations - 1))]


def _x(exp: str, row: dict[str, str]) -> tuple[str, float]:
    if exp == "exp1":
        return "num_agents", float(row["num_agents"])
    if exp == "exp2":
        return "gamma", float(row["gamma"])
    if exp == "exp3":
        return "affected_dependency_scope_percent", float(row["affected_scope_bucket_percent"])
    if row["failure_type"] == "capacity_degradation":
        return "post_fault_capacity_ratio", float(row["post_fault_capacity_ratio"])
    if row["failure_type"] == "agent_failure":
        return "dependency_closure_ratio", float(row["dependency_closure_ratio"])
    return "affected_flow_ratio", float(row["affected_flow_ratio"])


def aggregate_experiment(exp: str, raw_path: Path, output_path: Path) -> list[dict[str, object]]:
    rows = read_rows(raw_path)
    raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    groups: dict[tuple[str, str, float, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        method = row.get("method_id") or row.get("method") or ""
        x_name, x_value = _x(exp, row)
        series = row.get("failure_type", "") if exp == "exp4" else row.get("series", "")
        groups[(method, x_name, x_value, series)].append(row)
    out: list[dict[str, object]] = []
    for (method, x_name, x_value, series), items in sorted(groups.items()):
        raw_items = items
        if exp == "exp1":
            items = [
                row
                for row in raw_items
                if truth(row.get("paired_analysis_eligible", "true"))
            ]
        rate_metrics: dict[str, tuple[int, int]] = {
            "success_rate": (sum(truth(row.get("success")) for row in items), len(items)),
            "timeout_rate": (sum(truth(row.get("timeout")) for row in items), len(items)),
        }
        if exp == "exp1":
            rate_metrics.update({
                "paired_scenario_retention_rate": (
                    sum(
                        truth(row.get("paired_analysis_eligible", "true"))
                        for row in raw_items
                    ),
                    len(raw_items),
                ),
                "preparation_failure_rate": (
                    sum(
                        not truth(row.get("paired_analysis_eligible", "true"))
                        for row in raw_items
                    ),
                    len(raw_items),
                ),
            })
        if exp == "exp2":
            feasible = [row for row in items if row.get("conflict_class") != "UNRESOLVABLE_CONFLICT"]
            unresolvable = [row for row in items if row.get("conflict_class") == "UNRESOLVABLE_CONFLICT"]
            rate_metrics.update({
                "feasible_qos_satisfaction_rate": (sum(truth(row.get("qos_satisfied")) for row in feasible), len(feasible)),
                "pre_verification_correct_decision_rate": (sum(truth(row.get("pre_verification_correct_decision")) for row in items), len(items)),
                "system_safe_rejection_rate": (sum(truth(row.get("safe_rejection")) for row in unresolvable), len(unresolvable)),
                "rollback_rate": (sum(truth(row.get("rollback_triggered")) for row in items), len(items)),
                "verification_rescue_rate": (sum(truth(row.get("verification_rescued")) for row in items), len(items)),
                "unsafe_proposal_rate": (sum(truth(row.get("unsafe_proposal_before_verification")) for row in items), len(items)),
            })
        for metric, (num, den) in rate_metrics.items():
            estimate, low, high = wilson(num, den)
            out.append(_aggregate_row(exp, method, series, x_name, x_value, metric, estimate, low, high, num, den, raw_path, raw_sha, "wilson_95"))
        continuous = {
            "exp1": (("conditional_verified_latency_ms", "verified_formation_latency_s", 1000.0), ("route_install_latency_ms", "route_install_latency_s", 1000.0), ("control_messages", "control_messages", 1.0), ("rules_installed", "rules_installed", 1.0)),
            "exp2": (("coordination_latency_ms", "coordination_latency_ms", 1.0),),
            "exp3": (("conditional_verified_latency_ms", "reconfiguration_latency_ms", 1.0), ("modification_scope_ratio", "modification_scope_ratio", 1.0), ("unaffected_flow_disturbance", "unaffected_disturbance_ratio", 1.0)),
            "exp4": (("conditional_verified_latency_ms", "recovery_latency_ms", 1.0), ("modification_scope_ratio", "modification_scope_ratio", 1.0), ("unaffected_flow_interruption", "unaffected_disturbance_ratio", 1.0)),
        }[exp]
        for metric, field, factor in continuous:
            samples = [
                (int(row["seed"]), float(row[field]) * factor)
                for row in items if row.get(field, "") not in {"", None}
                and (not metric.startswith("conditional_") or truth(row.get("success")))
            ]
            estimate, low, high = cluster_bootstrap(samples)
            out.append(_aggregate_row(exp, method, series, x_name, x_value, metric, estimate, low, high, len(samples), len(items), raw_path, raw_sha, "topology_cluster_bootstrap_95"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(out[0]) if out else []
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(out)
    _write_paired_differences(exp, rows, output_path.with_name("paired_differences.csv"), raw_path, raw_sha)
    return out


def _write_paired_differences(exp: str, rows: list[dict[str, str]], path: Path, raw_path: Path, raw_sha: str) -> None:
    if exp == "exp1":
        rows = [
            row
            for row in rows
            if truth(row.get("paired_analysis_eligible", "true"))
        ]
    indexed: dict[tuple[str, float, str, str, str], dict[str, str]] = {}
    for row in rows:
        method = row.get("method_id") or row.get("method") or ""; x_name, x_value = _x(exp, row)
        series = row.get("failure_type", "") if exp == "exp4" else row.get("series", "")
        trial_identity = row.get("trial_id") or row.get("scenario_fingerprint") or ""
        # A scenario fingerprint identifies scenario content, not a unique
        # repeated trial.  Keep the topology seed in the pairing key so that
        # multiple seeds sharing identical content cannot overwrite each other.
        trial = f"{trial_identity}|seed={row['seed']}"
        indexed[(x_name, x_value, series, trial, method)] = row
    comparisons: dict[tuple[str, float, str, str, str], list[tuple[int, float]]] = defaultdict(list)
    metrics = [("success_rate", "success")]
    if exp == "exp2": metrics.append(("pre_verification_correct_decision_rate", "pre_verification_correct_decision"))
    continuous_metrics = (
        (
            ("conditional_verified_latency_ms", "verified_formation_latency_s", 1000.0, True),
            ("route_install_latency_ms", "route_install_latency_s", 1000.0, False),
        )
        if exp == "exp1"
        else ()
    )
    for (x_name, x_value, series, trial, method), row in indexed.items():
        if method == "proposed": continue
        reference = indexed.get((x_name, x_value, series, trial, "proposed"))
        if reference is None: continue
        for metric, field in metrics:
            comparisons[(x_name, x_value, series, method, metric)].append((int(row["seed"]), float(truth(row.get(field))) - float(truth(reference.get(field)))))
        for metric, field, factor, successful_runs_only in continuous_metrics:
            if successful_runs_only and not (
                truth(row.get("success")) and truth(reference.get("success"))
            ):
                continue
            if row.get(field, "") in {"", None} or reference.get(field, "") in {"", None}:
                continue
            difference = (
                float(row[field]) - float(reference[field])
            ) * factor
            comparisons[(x_name, x_value, series, method, metric)].append(
                (int(row["seed"]), difference)
            )
    output = []
    for (x_name, x_value, series, method, metric), values in sorted(comparisons.items()):
        estimate, low, high = cluster_bootstrap(values)
        output.append({"protocol_id": "wcnc_final_v3", "experiment": exp, "series": series,
            "method_id": method, "reference_method_id": "proposed", "x_name": x_name, "x_value": x_value,
            "metric": metric, "paired_difference": estimate, "ci_low": low, "ci_high": high,
            "paired_trials": len(values), "interval": "paired_topology_cluster_bootstrap_95",
            "raw_path": str(raw_path), "raw_sha256": raw_sha})
    if output:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(output[0]), lineterminator="\n"); writer.writeheader(); writer.writerows(output)


def _aggregate_row(exp, method, series, x_name, x_value, metric, estimate, low, high, numerator, denominator, raw_path, raw_sha, interval):
    return {"protocol_id": "wcnc_final_v3", "experiment": exp, "series": series,
        "method_id": method, "x_name": x_name, "x_value": x_value, "metric": metric,
        "estimate": estimate, "ci_low": low, "ci_high": high, "numerator": numerator,
        "denominator": denominator, "interval": interval, "raw_path": str(raw_path), "raw_sha256": raw_sha}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--root", default="results/paper/wcnc_final_v3"); parser.add_argument("--experiment", choices=("all", "exp1", "exp2", "exp3", "exp4"), default="all")
    args = parser.parse_args(); root = Path(args.root)
    experiments = ("exp1", "exp2", "exp3", "exp4") if args.experiment == "all" else (args.experiment,)
    for exp in experiments:
        aggregate_experiment(exp, root / "raw" / exp / "trials.csv", root / "aggregated" / exp / "metrics.csv")


if __name__ == "__main__": main()
