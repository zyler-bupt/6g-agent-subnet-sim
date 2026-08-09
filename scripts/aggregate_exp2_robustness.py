from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean, median, stdev
from typing import Iterable


BOOLEAN_METRICS = (
    "qos_satisfied",
    "infeasible_configuration",
    "conflict_detected",
    "conflict_resolved",
    "safe_rejection",
    "unsafe_execution",
    "decision_rejected",
    "rollback_triggered",
    "rollback_success",
    "timeout",
)
CONTINUOUS_METRICS = (
    "coordination_latency_ms",
    "feasibility_latency_ms",
    "transaction_latency_ms",
    "total_latency_ms",
    "peak_memory_mb",
    "num_raw_combinations",
    "num_pruned_combinations",
    "num_feasible_combinations",
)


def aggregate_robustness(results_dir: Path) -> dict[str, int]:
    raw_dir = results_dir / "raw"
    output_dir = results_dir / "processed"
    output_dir.mkdir(parents=True, exist_ok=True)
    runs = _read_csv(raw_dir / "runs.csv")
    main = _read_csv(raw_dir / "main_scenario_audit.csv")
    if not runs or not main:
        raise ValueError("robustness raw results are incomplete")

    scenario_summary = summarize_main_scenarios(main)
    _write_rows(output_dir / "scenario_specific_summary.csv", scenario_summary)
    robustness_summary = summarize_robustness(runs)
    _write_rows(output_dir / "summary.csv", robustness_summary)
    failures = [row for row in runs if not _bool(row["success"])]
    _write_rows(output_dir / "failures.csv", failures, list(runs[0]))
    conflict_classes = summarize_conflict_classes(main, runs)
    _write_rows(output_dir / "conflict_class_summary.csv", conflict_classes)
    pairwise = pairwise_tests(main, runs)
    _write_rows(output_dir / "pairwise_tests.csv", pairwise)
    difference_report = independent_noverification_report(main, runs)
    (output_dir / "independent_vs_noverification_report.json").write_text(
        json.dumps(difference_report, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return {
        "robustness_runs": len(runs),
        "main_audit_rows": len(main),
        "summary_rows": len(robustness_summary),
        "pairwise_rows": len(pairwise),
        "failures": len(failures),
    }


def summarize_main_scenarios(
    rows: list[dict[str, str]],
) -> list[dict[str, object]]:
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[(row["scenario"], row["conflict_pressure"], row["method"])].append(row)
    output = []
    for (scenario, pressure, method), group in sorted(groups.items()):
        output.append(
            {
                "scenario": scenario,
                "conflict_pressure": float(pressure),
                "method": method,
                "runs": len(group),
                "qos_satisfaction_rate": _mean_bool(group, "qos_satisfied"),
                "infeasible_configuration_rate": _mean_bool(
                    group, "infeasible_configuration"
                ),
                "conflict_detection_rate": _conditional_rate(
                    group,
                    "ground_truth_conflict",
                    "conflict_detected",
                ),
                "conflict_resolution_rate": _conditional_rate(
                    group,
                    "ground_truth_resolvable",
                    "conflict_resolved",
                ),
                "safe_rejection_rate": _mean_bool(group, "safe_rejection"),
                "rollback_rate": _mean_bool(group, "rollback_triggered"),
                **_mean_ci(group, "coordination_latency_ms"),
                **_mean_ci(group, "transaction_latency_ms"),
            }
        )
    return output


def summarize_robustness(
    rows: list[dict[str, str]],
) -> list[dict[str, object]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        factor = _factor(row)
        groups[(row["experiment"], row["scenario"], factor, row["method"])].append(row)
    output = []
    for (experiment, scenario, factor, method), group in sorted(groups.items()):
        item: dict[str, object] = {
            "experiment": experiment,
            "scenario": scenario,
            "factor": factor,
            "method": method,
            "runs": len(group),
            "conflict_class": _single_or_mixed(
                row["conflict_class"] for row in group
            ),
            "qos_satisfaction_rate": _mean_bool(group, "qos_satisfied"),
            "infeasible_configuration_rate": _mean_bool(
                group, "infeasible_configuration"
            ),
            "false_alarm_rate": _false_alarm_rate(group),
            "miss_rate": _miss_rate(group),
            "conflict_detection_rate": _conditional_rate(
                group, "ground_truth_conflict", "conflict_detected"
            ),
            "conflict_resolution_rate": _conditional_rate(
                group, "ground_truth_resolvable", "conflict_resolved"
            ),
            "safe_rejection_rate": _mean_bool(group, "safe_rejection"),
            "unresolvable_safe_rejection_rate": _conditional_class_rate(
                group, "UNRESOLVABLE_CONFLICT", "safe_rejection"
            ),
            "unsafe_execution_rate": _conditional_class_rate(
                group, "UNRESOLVABLE_CONFLICT", "unsafe_execution"
            ),
            "decision_rejection_rate": _mean_bool(group, "decision_rejected"),
            "rollback_rate": _mean_bool(group, "rollback_triggered"),
            "safe_rollback_rate": _conditional_rate(
                group, "rollback_triggered", "rollback_success"
            ),
            "timeout_rate": _mean_bool(group, "timeout"),
            "no_partial_commit_rate": _mean_bool(group, "no_partial_commit"),
        }
        for metric in CONTINUOUS_METRICS:
            item.update(_mean_ci(group, metric))
        output.append(item)
    return output


def summarize_conflict_classes(
    main: list[dict[str, str]],
    runs: list[dict[str, str]],
) -> list[dict[str, object]]:
    unique: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for row in main:
        key = (
            "main_exp2",
            row["scenario"],
            row["conflict_pressure"],
            row["seed"],
        )
        unique.setdefault(key, row)
    for row in runs:
        key = (
            row["experiment"],
            row["scenario"],
            _factor(row),
            row["seed"],
        )
        unique.setdefault(key, row)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for key, row in unique.items():
        counts[(key[0], row["conflict_class"])] += 1
    return [
        {"experiment": experiment, "conflict_class": category, "samples": count}
        for (experiment, category), count in sorted(counts.items())
    ]


def pairwise_tests(
    main: list[dict[str, str]],
    runs: list[dict[str, str]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    output.extend(
        _paired_rows(
            main,
            key_fields=("scenario", "conflict_pressure", "seed"),
            stratum_fields=("scenario", "conflict_pressure"),
            metrics=(
                "qos_satisfied",
                "infeasible_configuration",
                "conflict_detected",
                "rollback_triggered",
                "coordination_latency_ms",
                "transaction_latency_ms",
            ),
            experiment="main_exp2",
        )
    )
    by_experiment: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in runs:
        by_experiment[row["experiment"]].append(row)
    for experiment, experiment_rows in sorted(by_experiment.items()):
        for row in experiment_rows:
            row["pairing_factor"] = _factor(row)
        output.extend(
            _paired_rows(
                experiment_rows,
                key_fields=("scenario", "pairing_factor", "seed"),
                stratum_fields=("scenario", "pairing_factor"),
                metrics=(
                    "qos_satisfied",
                    "infeasible_configuration",
                    "conflict_detected",
                    "safe_rejection",
                    "rollback_triggered",
                    "timeout",
                    "coordination_latency_ms",
                    "transaction_latency_ms",
                    "total_latency_ms",
                    "peak_memory_mb",
                ),
                experiment=experiment,
            )
        )
    return output


def _paired_rows(
    rows: list[dict[str, str]],
    *,
    key_fields: tuple[str, ...],
    stratum_fields: tuple[str, ...],
    metrics: tuple[str, ...],
    experiment: str,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, ...], dict[str, dict[str, str]]] = defaultdict(dict)
    for row in rows:
        grouped[tuple(row[field] for field in key_fields)][row["method"]] = row
    strata: dict[tuple[str, ...], list[dict[str, dict[str, str]]]] = defaultdict(list)
    for key, methods in grouped.items():
        stratum = tuple(key[key_fields.index(field)] for field in stratum_fields)
        strata[stratum].append(methods)
    output = []
    for stratum, cases in sorted(strata.items()):
        all_methods = sorted({method for case in cases for method in case})
        if "proposed" not in all_methods:
            continue
        for method_b in ("independent", "adjacent", "no_verification"):
            if method_b not in all_methods:
                continue
            paired = [
                (case["proposed"], case[method_b])
                for case in cases
                if "proposed" in case and method_b in case
            ]
            for metric in metrics:
                if not paired or metric not in paired[0][0] or metric not in paired[0][1]:
                    continue
                if metric in BOOLEAN_METRICS:
                    statistics = _mcnemar(paired, metric)
                else:
                    statistics = _wilcoxon(paired, metric)
                output.append(
                    {
                        "experiment": experiment,
                        "scenario": stratum[0] if stratum else "",
                        "factor": stratum[1] if len(stratum) > 1 else "",
                        "metric": metric,
                        "method_a": "proposed",
                        "method_b": method_b,
                        **statistics,
                    }
                )
    return output


def _mcnemar(
    pairs: list[tuple[dict[str, str], dict[str, str]]],
    metric: str,
) -> dict[str, object]:
    values = [
        (int(_bool(left[metric])), int(_bool(right[metric])))
        for left, right in pairs
    ]
    differences = [left - right for left, right in values]
    b = sum(left == 1 and right == 0 for left, right in values)
    c = sum(left == 0 and right == 1 for left, right in values)
    discordant = b + c
    p_value = 1.0
    if discordant:
        tail = sum(
            math.comb(discordant, index)
            for index in range(0, min(b, c) + 1)
        ) / (2**discordant)
        p_value = min(1.0, 2.0 * tail)
    return {
        "mean_difference": fmean(differences),
        "median_difference": median(differences),
        "95_percent_confidence_interval": _ci_text(differences),
        "paired_test": "exact_McNemar",
        "p_value": p_value,
        "effect_size": (b - c) / discordant if discordant else 0.0,
        "effect_size_type": "discordant_pair_difference",
        "sample_size": len(values),
    }


def _wilcoxon(
    pairs: list[tuple[dict[str, str], dict[str, str]]],
    metric: str,
) -> dict[str, object]:
    differences = [float(left[metric]) - float(right[metric]) for left, right in pairs]
    nonzero = [value for value in differences if abs(value) > 1e-15]
    if not nonzero:
        return {
            "mean_difference": 0.0,
            "median_difference": 0.0,
            "95_percent_confidence_interval": "[0, 0]",
            "paired_test": "Wilcoxon_signed_rank_normal_approximation",
            "p_value": 1.0,
            "effect_size": 0.0,
            "effect_size_type": "rank_biserial_correlation",
            "sample_size": len(differences),
        }
    ranks = _average_ranks([abs(value) for value in nonzero])
    positive = sum(rank for value, rank in zip(nonzero, ranks) if value > 0)
    negative = sum(rank for value, rank in zip(nonzero, ranks) if value < 0)
    n = len(nonzero)
    expected = n * (n + 1) / 4.0
    variance = n * (n + 1) * (2 * n + 1) / 24.0
    z = (min(positive, negative) - expected + 0.5) / math.sqrt(variance)
    p_value = min(1.0, math.erfc(abs(z) / math.sqrt(2.0)))
    total_rank = positive + negative
    return {
        "mean_difference": fmean(differences),
        "median_difference": median(differences),
        "95_percent_confidence_interval": _ci_text(differences),
        "paired_test": "Wilcoxon_signed_rank_normal_approximation",
        "p_value": p_value,
        "effect_size": (
            (positive - negative) / total_rank if total_rank else 0.0
        ),
        "effect_size_type": "rank_biserial_correlation",
        "sample_size": len(differences),
    }


def independent_noverification_report(
    main: list[dict[str, str]],
    runs: list[dict[str, str]],
) -> dict[str, object]:
    paired: list[tuple[dict[str, str], dict[str, str]]] = []
    groups: dict[tuple[str, ...], dict[str, dict[str, str]]] = defaultdict(dict)
    for row in main:
        groups[
            ("main_exp2", row["scenario"], row["conflict_pressure"], row["seed"])
        ][row["method"]] = row
    for row in runs:
        groups[
            (row["experiment"], row["scenario"], _factor(row), row["seed"])
        ][row["method"]] = row
    for methods in groups.values():
        if "independent" in methods and "no_verification" in methods:
            paired.append((methods["independent"], methods["no_verification"]))
    same_selection = sum(
        left.get("selected_proposal_ids", left.get("selected_proposals", ""))
        == right.get("selected_proposal_ids", right.get("selected_proposals", ""))
        for left, right in paired
    )
    same_actions = sum(
        left.get("selected_action_set", "") == right.get("selected_action_set", "")
        for left, right in paired
    )
    examples = []
    for left, right in paired:
        left_ids = left.get("selected_proposal_ids", left.get("selected_proposals", ""))
        right_ids = right.get("selected_proposal_ids", right.get("selected_proposals", ""))
        if left_ids == right_ids or len(examples) >= 10:
            continue
        examples.append(
            {
                "scenario": left["scenario"],
                "seed": int(left["seed"]),
                "independent_selection": left_ids,
                "no_verification_selection": right_ids,
                "independent_actions": left.get("selected_action_set", ""),
                "no_verification_actions": right.get("selected_action_set", ""),
                "independent_rollback": _bool(left.get("rollback_triggered", "False")),
                "no_verification_rollback": _bool(right.get("rollback_triggered", "False")),
                "independent_failure": left.get("failure_reason", ""),
                "no_verification_failure": right.get("failure_reason", ""),
            }
        )
    count = len(paired)
    return {
        "status": "PASS" if count and same_selection < count else "FAIL",
        "paired_runs": count,
        "same_selection_count": same_selection,
        "same_selection_rate": same_selection / count if count else 0.0,
        "same_action_set_count": same_actions,
        "same_action_set_rate": same_actions / count if count else 0.0,
        "independent_definition": (
            "each layer independently selects maximum local utility; no joint score "
            "and no joint feasibility check"
        ),
        "no_verification_definition": (
            "enumerates joint combinations and ranks declared joint score; skips "
            "hard pre-execution feasibility and relies on the common post verifier"
        ),
        "mechanism_difference_verified": same_selection < count,
        "examples": examples,
    }


def _factor(row: dict[str, str]) -> str:
    experiment = row["experiment"]
    if experiment == "noise":
        return row["noise_ratio"]
    if experiment == "stale":
        return row["stale_ms"]
    if experiment == "missing_layer":
        return row["missing_layer"]
    if experiment == "proposal_scale":
        return row["proposals_per_layer"]
    return row["scenario"]


def _mean_ci(group: list[dict[str, str]], metric: str) -> dict[str, float]:
    values = [float(row[metric]) for row in group]
    deviation = stdev(values) if len(values) > 1 else 0.0
    return {
        f"{metric}_mean": fmean(values),
        f"{metric}_ci95": 1.96 * deviation / math.sqrt(len(values)),
    }


def _mean_bool(group: list[dict[str, str]], metric: str) -> float:
    return sum(_bool(row[metric]) for row in group) / len(group)


def _conditional_rate(
    group: list[dict[str, str]],
    condition: str,
    outcome: str,
) -> float:
    selected = [row for row in group if _bool(row[condition])]
    return _mean_bool(selected, outcome) if selected else 0.0


def _conditional_class_rate(
    group: list[dict[str, str]],
    category: str,
    outcome: str,
) -> float:
    selected = [row for row in group if row["conflict_class"] == category]
    return _mean_bool(selected, outcome) if selected else 0.0


def _false_alarm_rate(group: list[dict[str, str]]) -> float:
    selected = [row for row in group if not _bool(row["ground_truth_conflict"])]
    return _mean_bool(selected, "conflict_detected") if selected else 0.0


def _miss_rate(group: list[dict[str, str]]) -> float:
    selected = [row for row in group if _bool(row["ground_truth_conflict"])]
    if not selected:
        return 0.0
    return sum(not _bool(row["conflict_detected"]) for row in selected) / len(selected)


def _ci_text(values: Iterable[float]) -> str:
    materialized = list(values)
    if not materialized:
        return "[0, 0]"
    deviation = stdev(materialized) if len(materialized) > 1 else 0.0
    margin = 1.96 * deviation / math.sqrt(len(materialized))
    mean = fmean(materialized)
    return f"[{mean - margin:.12g}, {mean + margin:.12g}]"


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and math.isclose(
            ordered[end][1], ordered[index][1], rel_tol=1e-12, abs_tol=1e-15
        ):
            end += 1
        rank = ((index + 1) + end) / 2.0
        for position in range(index, end):
            ranks[ordered[position][0]] = rank
        index = end
    return ranks


def _single_or_mixed(values: Iterable[str]) -> str:
    unique = sorted(set(values))
    return unique[0] if len(unique) == 1 else "MIXED"


def _bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(
    path: Path,
    rows: list[dict[str, object]] | list[dict[str, str]],
    fields: list[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = fields or (list(rows[0]) if rows else [])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate Exp2 robustness results")
    parser.add_argument("--results-dir", default="results/exp2_robustness")
    return parser


def main() -> None:
    args = _parser().parse_args()
    counts = aggregate_robustness(Path(args.results_dir))
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
