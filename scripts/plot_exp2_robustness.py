from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean, stdev

import scripts.plot_exp2 as base_plot


base_plot.COLORS.update(
    {
        "raw_combinations": "#0F172A",
        "proposed_evaluated": "#2563EB",
        "adjacent_evaluated": "#16A34A",
        "same_selection": "#64748B",
        "different_selection": "#F59E0B",
    }
)


def plot_robustness(results_dir: Path) -> list[Path]:
    raw = results_dir / "raw"
    processed = results_dir / "processed"
    figures = results_dir / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    runs = _read_csv(raw / "runs.csv")
    main = _read_csv(raw / "main_scenario_audit.csv")
    summary = _read_csv(processed / "summary.csv")
    difference = json.loads(
        (processed / "independent_vs_noverification_report.json").read_text(
            encoding="utf-8"
        )
    )
    outputs: list[Path] = []

    scenario_qos = _scenario_qos(main)
    outputs.extend(
        _line_figure(
            figures,
            "fig_scenario_specific_qos",
            scenario_qos,
            "Scenario-Specific QoS Satisfaction",
            "Scenario / conflict pressure",
            "QoS satisfaction rate",
        )
    )

    by_class = _class_outcomes(main, runs)
    outputs.extend(
        _line_figure(
            figures,
            "fig_resolvable_vs_unresolvable",
            by_class,
            "No Conflict, Resolvable, and Unresolvable Cases",
            "Ground Truth class",
            "QoS satisfaction rate",
        )
    )

    safe = _safe_rejection(runs)
    outputs.extend(
        _line_figure(
            figures,
            "fig_safe_rejection_rate",
            safe,
            "Safe Handling of Unresolvable Conflicts",
            "Unresolvable mechanism",
            "Safe rejection / rollback rate",
        )
    )

    noise = _grouped_rate_rows(
        runs,
        experiment="noise",
        factor="noise_ratio",
        outcome="qos_satisfied",
    )
    outputs.extend(
        _line_figure(
            figures,
            "fig_noise_robustness",
            noise,
            "Robustness to Observation Noise",
            "Observation noise sigma",
            "QoS satisfaction rate",
        )
    )

    stale = _grouped_rate_rows(
        runs,
        experiment="stale",
        factor="stale_ms",
        outcome="qos_satisfied",
    )
    outputs.extend(
        _line_figure(
            figures,
            "fig_stale_state_robustness",
            stale,
            "Robustness to Stale State",
            "Staleness (ms)",
            "QoS satisfaction rate",
        )
    )

    missing = _missing_rows(runs)
    outputs.extend(
        _line_figure(
            figures,
            "fig_missing_layer_ablation",
            missing,
            "Missing-Layer Conservative Handling",
            "Missing observation",
            "Safe outcome rate",
        )
    )

    scale_latency = _scale_latency(summary)
    outputs.extend(
        _line_figure(
            figures,
            "fig_coordination_latency_vs_proposals",
            scale_latency,
            "Coordination Latency vs. Proposals per Layer",
            "Proposals per layer",
            "Coordination latency (ms)",
        )
    )

    growth = _combination_growth(runs)
    outputs.extend(
        _line_figure(
            figures,
            "fig_candidate_combination_growth",
            growth,
            "Candidate Combination Growth",
            "Proposals per layer",
            "Combination count",
        )
    )

    selection = [
        {
            "method": "same_selection",
            "mean": float(difference["same_selection_rate"]),
            "ci95": 0.0,
            "sample_count": int(difference["paired_runs"]),
        },
        {
            "method": "different_selection",
            "mean": 1.0 - float(difference["same_selection_rate"]),
            "ci95": 0.0,
            "sample_count": int(difference["paired_runs"]),
        },
    ]
    name = "fig_independent_vs_noverify_selection"
    csv_path = figures / f"{name}.csv"
    base_plot._write_rows(csv_path, selection)
    outputs.append(csv_path)
    outputs.extend(
        base_plot._bar_chart(
            figures / name,
            selection,
            title="Independent vs. No-Verification Selection",
            y_label="Paired-run rate",
        )
    )
    return outputs


def _line_figure(
    output_dir: Path,
    name: str,
    rows: list[dict[str, object]],
    title: str,
    x_label: str,
    y_label: str,
) -> list[Path]:
    csv_path = output_dir / f"{name}.csv"
    base_plot._write_rows(csv_path, rows)
    return [
        csv_path,
        *base_plot._line_chart(
            output_dir / name,
            rows,
            title=title,
            x_label=x_label,
            y_label=y_label,
        ),
    ]


def _scenario_qos(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    order = {
        "application_capacity": "A",
        "transport_network": "T",
        "network_physical": "N",
    }
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[(row["scenario"], row["conflict_pressure"], row["method"])].append(row)
    output = []
    for (scenario, pressure, method), group in sorted(groups.items()):
        values = [float(_bool(row["qos_satisfied"])) for row in group]
        output.append(
            {
                "x": f"{order[scenario]}-{float(pressure):g}",
                "scenario": scenario,
                "pressure": float(pressure),
                "method": method,
                "mean": fmean(values),
                "ci95": _ci95(values),
                "sample_count": len(values),
            }
        )
    return output


def _class_outcomes(
    main: list[dict[str, str]],
    runs: list[dict[str, str]],
) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in main:
        groups[(row["conflict_class"], row["method"])].append(
            float(_bool(row["qos_satisfied"]))
        )
    for row in runs:
        if row["experiment"] != "unresolvable":
            continue
        groups[(row["conflict_class"], row["method"])].append(
            float(_bool(row["qos_satisfied"]))
        )
    order = ("NO_CONFLICT", "RESOLVABLE_CONFLICT", "UNRESOLVABLE_CONFLICT")
    output = []
    for (category, method), values in groups.items():
        output.append(
            {
                "x": category,
                "conflict_class": category,
                "method": method,
                "mean": fmean(values),
                "ci95": _ci95(values),
                "sample_count": len(values),
            }
        )
    return sorted(output, key=lambda row: (order.index(str(row["x"])), str(row["method"])))


def _safe_rejection(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        if row["experiment"] == "unresolvable":
            groups[(row["scenario"], row["method"])].append(
                float(_bool(row["safe_rejection"]))
            )
    output = []
    for (scenario, method), values in sorted(groups.items()):
        output.append(
            {
                "x": scenario.replace("_", "-"),
                "scenario": scenario,
                "method": method,
                "mean": fmean(values),
                "ci95": _ci95(values),
                "sample_count": len(values),
            }
        )
    return output


def _grouped_rate_rows(
    rows: list[dict[str, str]],
    *,
    experiment: str,
    factor: str,
    outcome: str,
) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["experiment"] == experiment:
            groups[(row[factor], row["method"])].append(row)
    output = []
    for (value, method), group in sorted(
        groups.items(), key=lambda item: (float(item[0][0]), item[0][1])
    ):
        values = [float(_bool(row[outcome])) for row in group]
        output.append(
            {
                "x": f"{float(value):g}",
                factor: float(value),
                "method": method,
                "mean": fmean(values),
                "ci95": _ci95(values),
                "sample_count": len(values),
                "infeasible_rate": fmean(
                    float(_bool(row["infeasible_configuration"])) for row in group
                ),
                "false_alarm_rate": _conditional_detection(group, truth=False),
                "miss_rate": _conditional_detection(group, truth=True, miss=True),
                "safe_rejection_rate": fmean(
                    float(_bool(row["safe_rejection"])) for row in group
                ),
            }
        )
    return output


def _missing_rows(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["experiment"] == "missing_layer":
            groups[(row["missing_layer"], row["method"])].append(row)
    order = ("application", "transport", "network", "physical")
    output = []
    for (layer, method), group in groups.items():
        safe = [
            float(
                _bool(row["qos_satisfied"])
                or _bool(row["safe_rejection"])
                or (
                    _bool(row["rollback_triggered"])
                    and _bool(row["rollback_success"])
                )
            )
            for row in group
        ]
        output.append(
            {
                "x": layer,
                "missing_layer": layer,
                "method": method,
                "mean": fmean(safe),
                "ci95": _ci95(safe),
                "sample_count": len(safe),
                "qos_satisfaction_rate": fmean(
                    float(_bool(row["qos_satisfied"])) for row in group
                ),
                "infeasible_configuration_rate": fmean(
                    float(_bool(row["infeasible_configuration"])) for row in group
                ),
                "safe_rejection_rate": fmean(
                    float(_bool(row["safe_rejection"])) for row in group
                ),
            }
        )
    return sorted(output, key=lambda row: (order.index(str(row["x"])), str(row["method"])))


def _scale_latency(summary: list[dict[str, str]]) -> list[dict[str, object]]:
    output = []
    for row in summary:
        if row["experiment"] != "proposal_scale":
            continue
        output.append(
            {
                "x": f"{int(float(row['factor']))}",
                "proposals_per_layer": int(float(row["factor"])),
                "method": row["method"],
                "mean": float(row["coordination_latency_ms_mean"]),
                "ci95": float(row["coordination_latency_ms_ci95"]),
                "sample_count": int(row["runs"]),
                "peak_memory_mb": float(row["peak_memory_mb_mean"]),
                "timeout_rate": float(row["timeout_rate"]),
            }
        )
    return sorted(output, key=lambda row: (int(row["proposals_per_layer"]), str(row["method"])))


def _combination_growth(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    groups: dict[tuple[int, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["experiment"] == "proposal_scale":
            groups[(int(row["proposals_per_layer"]), row["method"])].append(row)
    output = []
    for count in range(1, 6):
        proposed = groups.get((count, "proposed"), [])
        adjacent = groups.get((count, "adjacent"), [])
        if proposed:
            output.append(
                {
                    "x": str(count),
                    "proposals_per_layer": count,
                    "method": "raw_combinations",
                    "mean": fmean(float(row["num_raw_combinations"]) for row in proposed),
                    "ci95": 0.0,
                    "sample_count": len(proposed),
                }
            )
            output.append(
                {
                    "x": str(count),
                    "proposals_per_layer": count,
                    "method": "proposed_evaluated",
                    "mean": fmean(float(row["num_evaluated_combinations"]) for row in proposed),
                    "ci95": 0.0,
                    "sample_count": len(proposed),
                }
            )
        if adjacent:
            output.append(
                {
                    "x": str(count),
                    "proposals_per_layer": count,
                    "method": "adjacent_evaluated",
                    "mean": fmean(float(row["num_evaluated_combinations"]) for row in adjacent),
                    "ci95": 0.0,
                    "sample_count": len(adjacent),
                }
            )
    return output


def _conditional_detection(
    rows: list[dict[str, str]],
    *,
    truth: bool,
    miss: bool = False,
) -> float:
    selected = [
        row for row in rows if _bool(row["ground_truth_conflict"]) == truth
    ]
    if not selected:
        return 0.0
    if miss:
        return fmean(float(not _bool(row["conflict_detected"])) for row in selected)
    return fmean(float(_bool(row["conflict_detected"])) for row in selected)


def _ci95(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    return 1.96 * stdev(values) / math.sqrt(len(values))


def _bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plot Exp2 robustness results")
    parser.add_argument("--results-dir", default="results/exp2_robustness")
    return parser


def main() -> None:
    args = _parser().parse_args()
    outputs = plot_robustness(Path(args.results_dir))
    print(f"Wrote {len(outputs)} figure artifacts")


if __name__ == "__main__":
    main()
