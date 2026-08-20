from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Sequence

import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.exp1_initial_formation import run_exp1
from experiments.exp2_conflict import run_exp2
from experiments.exp3_business_elasticity import run_exp3
from experiments.exp4_failure import run_exp4
from experiments.paper_protocol import METHODS
from scripts.aggregate_results import aggregate_experiment
from scripts.plot_final_paper_figures import plot_exp1, plot_exp2, plot_exp3, plot_exp4
from scripts.sanity_check_results import SanityFinding, check_results


@dataclass(frozen=True)
class PilotRunResult:
    experiments: tuple[str, ...]
    raw_trial_count: int
    aggregate_row_count: int
    error_count: int
    warning_count: int
    output_root: Path


async def run_pilot(
    experiments: Sequence[str] = ("exp1",),
    *,
    output_root: Path = Path("results"),
    seeds: tuple[int, ...] | None = None,
    exp1_task_sizes: tuple[int, ...] = (8, 12, 16, 20, 24, 28, 32),
    exp1_churn_points: tuple[int | float, ...] = (0, 5, 10, 15, 20, 30),
    exp2_event_ids: tuple[int, ...] | None = None,
    exp2_conflict_densities: tuple[int, ...] = (0, 10, 20, 30, 40, 50, 60),
    exp3_buckets: tuple[int, ...] = (10, 20, 30, 40, 50),
    exp4_event_ids: tuple[int, ...] | None = None,
    exp4_capacity_reductions: tuple[int, ...] = (10, 20, 30, 40, 50),
    bootstrap_iterations: int = 5000,
    replace_existing: bool = False,
) -> PilotRunResult:
    selected = tuple(dict.fromkeys(experiments))
    unsupported = set(selected) - {"exp1", "exp2", "exp3", "exp4"}
    if unsupported:
        raise ValueError(
            "pilot orchestration is not implemented yet for: "
            + ", ".join(sorted(unsupported))
        )
    if len(selected) != 1:
        raise ValueError("run one experiment per audited pilot invocation")
    experiment = selected[0]

    raw_path = output_root / "raw" / "pilot" / experiment / "trials.csv"
    aggregate_dir = output_root / "aggregated" / "pilot" / experiment
    summary_path = aggregate_dir / "summary.csv"
    figure_dir = output_root / "paper_figures"
    if raw_path.exists() and not replace_existing:
        raise FileExistsError(
            f"authoritative pilot already exists at {raw_path}; use --replace-pilot "
            "to rerun every method"
        )
    if replace_existing:
        _clear_pilot(output_root, experiment)

    if experiment == "exp1":
        rows = await run_exp1(
            "pilot",
            output_root,
            seeds=seeds,
            task_sizes=exp1_task_sizes,
            churn_points=exp1_churn_points,
        )
    elif experiment == "exp2":
        rows = await run_exp2(
            "pilot",
            output_root,
            seeds=seeds,
            event_ids=exp2_event_ids,
            conflict_densities=exp2_conflict_densities,
        )
    elif experiment == "exp3":
        rows = await run_exp3(
            "pilot",
            output_root,
            seeds=seeds,
            buckets=exp3_buckets,
        )
    else:
        rows = await run_exp4(
            "pilot",
            output_root,
            seeds=seeds,
            event_ids=exp4_event_ids,
            capacity_reductions=exp4_capacity_reductions,
        )
    aggregate_rows = aggregate_experiment(
        raw_path,
        summary_path,
        bootstrap_iterations=bootstrap_iterations,
    )
    findings = check_results(raw_path, experiment=experiment)
    aggregate_dir.mkdir(parents=True, exist_ok=True)
    with (aggregate_dir / "sanity.json").open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as handle:
        json.dump(
            [asdict(item) for item in findings],
            handle,
            indent=2,
            ensure_ascii=False,
        )
        handle.write("\n")
    if experiment == "exp1":
        plot_exp1(summary_path, figure_dir)
        report = _exp1_pilot_report(rows, aggregate_rows, findings)
        payload = _exp1_pilot_summary_payload(rows, aggregate_rows, findings)
    elif experiment == "exp2":
        plot_exp2(summary_path, figure_dir)
        report = _exp2_pilot_report(rows, aggregate_rows, findings)
        payload = _exp2_pilot_summary_payload(rows, aggregate_rows, findings)
    elif experiment == "exp3":
        plot_exp3(summary_path, figure_dir)
        report = _exp3_pilot_report(rows, aggregate_rows, findings)
        payload = _exp3_pilot_summary_payload(rows, aggregate_rows, findings)
    else:
        plot_exp4(summary_path, figure_dir)
        report = _exp4_pilot_report(rows, aggregate_rows, findings)
        payload = _exp4_pilot_summary_payload(rows, aggregate_rows, findings)
    (aggregate_dir / "PILOT_SUMMARY.md").write_text(report, encoding="utf-8")
    with (aggregate_dir / "PILOT_SUMMARY.json").open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as handle:
        json.dump(
            payload,
            handle,
            indent=2,
            ensure_ascii=False,
        )
        handle.write("\n")

    return PilotRunResult(
        experiments=selected,
        raw_trial_count=len(rows),
        aggregate_row_count=len(aggregate_rows),
        error_count=sum(item.level == "ERROR" for item in findings),
        warning_count=sum(item.level == "WARNING" for item in findings),
        output_root=output_root,
    )


def _exp1_pilot_report(rows, aggregate_rows, findings: Sequence[SanityFinding]) -> str:
    payload = _exp1_pilot_summary_payload(rows, aggregate_rows, findings)
    lines = [
        "# Exp.1 Pilot Summary",
        "",
        "> Pilot only — these values are for implementation and stress-range checks, not paper results.",
        "",
        f"- Raw trials: {payload['raw_trial_count']}",
        f"- Topology seeds: {payload['topology_seed_count']}",
        f"- Sanity errors: {payload['error_count']}",
        f"- Sanity warnings: {payload['warning_count']}",
        "",
        "## Method Ranking",
        "",
        "| Latency Rank | Method | Mean Formation Latency (ms) | P95 Formation Latency (ms) | Churn Success Rate (%) |",
        "|---:|---|---:|---:|---:|",
    ]
    for index, item in enumerate(payload["latency_ranking"], start=1):
        lines.append(
            f"| {index} | {item['method_label']} | {item['mean_formation_latency_ms']:.3f} "
            f"| {item['p95_formation_latency_ms']:.3f} | {item['churn_success_rate_percent']:.1f} |"
        )
    lines.extend(("", "## Sanity Findings", ""))
    if findings:
        lines.extend(
            f"- [{item.level}] `{item.code}`: {item.message}"
            for item in findings
        )
    else:
        lines.append("- No findings.")
    lines.extend(
        (
            "",
            "## Outputs",
            "",
            "- Raw CSV: `results/raw/pilot/exp1/trials.csv`",
            "- Aggregate CSV: `results/aggregated/pilot/exp1/summary.csv`",
            "- Figure: `results/paper_figures/Fig1_Formation.{pdf,png}`",
            "",
        )
    )
    return "\n".join(lines)


def _exp1_pilot_summary_payload(rows, aggregate_rows, findings) -> dict[str, object]:
    latency_by_method: dict[str, list[float]] = {}
    success_by_method: dict[str, list[bool]] = {}
    for row in rows:
        if row.series == "task_size" and row.formation_latency_ms is not None:
            latency_by_method.setdefault(row.method_id, []).append(
                row.formation_latency_ms
            )
        if row.series == "state_churn" and row.success is not None:
            success_by_method.setdefault(row.method_id, []).append(row.success)
    ranking = []
    for method_id, values in latency_by_method.items():
        successes = success_by_method.get(method_id, [])
        ranking.append(
            {
                "method_id": method_id,
                "method_label": METHODS[method_id].label,
                "mean_formation_latency_ms": mean(values),
                "p95_formation_latency_ms": float(np.percentile(values, 95.0)),
                "churn_success_rate_percent": (
                    100.0 * sum(successes) / len(successes) if successes else 0.0
                ),
            }
        )
    ranking.sort(key=lambda item: (item["mean_formation_latency_ms"], item["method_id"]))
    return {
        "experiment": "exp1",
        "mode": "pilot",
        "raw_trial_count": len(rows),
        "aggregate_row_count": len(aggregate_rows),
        "topology_seed_count": len({row.seed for row in rows}),
        "error_count": sum(item.level == "ERROR" for item in findings),
        "warning_count": sum(item.level == "WARNING" for item in findings),
        "latency_ranking": ranking,
        "findings": [asdict(item) for item in findings],
    }


def _exp2_pilot_report(rows, aggregate_rows, findings: Sequence[SanityFinding]) -> str:
    payload = _exp2_pilot_summary_payload(rows, aggregate_rows, findings)
    lines = [
        "# Exp.2 Pilot Summary",
        "",
        "> Pilot only — these values are for implementation and stress-range checks, not paper results.",
        "",
        f"- Raw trials: {payload['raw_trial_count']}",
        f"- Topology seeds: {payload['topology_seed_count']}",
        f"- Oracle-solvable instances: {payload['solvable_instance_count']} / {payload['instance_count']} ({payload['solvable_rate_percent']:.1f}%)",
        f"- Sanity errors: {payload['error_count']}",
        f"- Sanity warnings: {payload['warning_count']}",
        "",
        "## Conditional Coordination Results",
        "",
        "| Method | Feasible Solution Rate (%) | QoS Satisfaction Rate (%) | Safe Rejection Rate (%) | P95 Resolution Latency (ms) |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in payload["method_results"]:
        lines.append(
            f"| {item['method_label']} | {item['feasible_solution_rate_percent']:.1f} "
            f"| {item['qos_satisfaction_rate_percent']:.1f} "
            f"| {_format_optional(item['safe_rejection_rate_percent'])} "
            f"| {item['p95_resolution_latency_ms']:.3f} |"
        )
    lines.extend(("", "## Sanity Findings", ""))
    if findings:
        lines.extend(
            f"- [{item.level}] `{item.code}`: {item.message}"
            for item in findings
        )
    else:
        lines.append("- No findings.")
    lines.extend(
        (
            "",
            "## Outputs",
            "",
            "- Raw CSV: `results/raw/pilot/exp2/trials.csv`",
            "- Aggregate CSV: `results/aggregated/pilot/exp2/summary.csv`",
            "- Figure: `results/paper_figures/Fig2_Cross_Layer_Coordination.{pdf,png}`",
            "- Safe Rejection is retained in CSV/report and omitted from the compact two-panel main figure.",
            "",
        )
    )
    return "\n".join(lines)


def _exp2_pilot_summary_payload(rows, aggregate_rows, findings) -> dict[str, object]:
    unique_instances = {}
    for row in rows:
        unique_instances.setdefault(row.trial_id, row)
    method_results = []
    for method_id in ("proposed", "sanet_dw", "adjacent_layer", "independent"):
        selected = [row for row in rows if row.method_id == method_id]
        solvable = [row for row in selected if row.ground_truth_feasible]
        infeasible = [row for row in selected if not row.ground_truth_feasible]
        latencies = [row.resolution_latency_ms for row in selected]
        method_results.append(
            {
                "method_id": method_id,
                "method_label": METHODS[method_id].label,
                "feasible_solution_rate_percent": 100.0
                * sum(bool(row.success) for row in solvable)
                / len(solvable),
                "qos_satisfaction_rate_percent": 100.0
                * sum(bool(row.qos_satisfied) for row in solvable)
                / len(solvable),
                "safe_rejection_rate_percent": (
                    100.0
                    * sum(bool(row.safe_rejection) for row in infeasible)
                    / len(infeasible)
                    if infeasible
                    else None
                ),
                "p95_resolution_latency_ms": float(np.percentile(latencies, 95.0)),
            }
        )
    solvable_count = sum(
        bool(row.ground_truth_feasible) for row in unique_instances.values()
    )
    return {
        "experiment": "exp2",
        "mode": "pilot",
        "raw_trial_count": len(rows),
        "aggregate_row_count": len(aggregate_rows),
        "topology_seed_count": len({row.seed for row in rows}),
        "instance_count": len(unique_instances),
        "solvable_instance_count": solvable_count,
        "solvable_rate_percent": 100.0 * solvable_count / len(unique_instances),
        "error_count": sum(item.level == "ERROR" for item in findings),
        "warning_count": sum(item.level == "WARNING" for item in findings),
        "method_results": method_results,
        "findings": [asdict(item) for item in findings],
    }


def _exp3_pilot_report(rows, aggregate_rows, findings: Sequence[SanityFinding]) -> str:
    payload = _exp3_pilot_summary_payload(rows, aggregate_rows, findings)
    lines = [
        "# Exp.3 Pilot Summary",
        "",
        "> Pilot only — these values are for implementation and stress-range checks, not paper results.",
        "",
        f"- Raw trials: {payload['raw_trial_count']}",
        f"- Topology seeds: {payload['topology_seed_count']}",
        f"- Sanity errors: {payload['error_count']}",
        f"- Sanity warnings: {payload['warning_count']}",
        "",
        "## Method Ranking and Trade-off",
        "",
        "| Method | Mean Successful Latency (ms) | P95 Latency (ms) | Rule Change Ratio (%) | Success Rate (%) | Unaffected Disturbance (%) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in payload["method_tradeoff"]:
        lines.append(
            f"| {item['method_label']} | {_format_optional(item['mean_reconfiguration_latency_ms'])} "
            f"| {_format_optional(item['p95_reconfiguration_latency_ms'])} "
            f"| {item['mean_rule_change_ratio_percent']:.1f} "
            f"| {item['success_rate_percent']:.1f} "
            f"| {item['mean_unaffected_disturbance_percent']:.1f} |"
        )
    lines.extend(("", "## Sanity Findings", ""))
    if findings:
        lines.extend(
            f"- [{item.level}] `{item.code}`: {item.message}"
            for item in findings
        )
    else:
        lines.append("- No findings.")
    lines.extend(
        (
            "",
            "## Outputs",
            "",
            "- Raw CSV: `results/raw/pilot/exp3/trials.csv`",
            "- Aggregate CSV: `results/aggregated/pilot/exp3/summary.csv`",
            "- Figure: `results/paper_figures/Fig3_Business_Elasticity.{pdf,png}`",
            "",
        )
    )
    return "\n".join(lines)


def _exp3_pilot_summary_payload(rows, aggregate_rows, findings) -> dict[str, object]:
    tradeoff = []
    for method_id in ("proposed", "netren", "local_only", "full_rebuild"):
        selected = [row for row in rows if row.method_id == method_id]
        latencies = [
            row.reconfiguration_latency_ms
            for row in selected
            if row.reconfiguration_latency_ms is not None
        ]
        tradeoff.append(
            {
                "method_id": method_id,
                "method_label": METHODS[method_id].label,
                "mean_reconfiguration_latency_ms": (
                    mean(latencies) if latencies else None
                ),
                "p95_reconfiguration_latency_ms": (
                    float(np.percentile(latencies, 95.0)) if latencies else None
                ),
                "mean_rule_change_ratio_percent": 100.0
                * mean(row.rule_change_ratio for row in selected),
                "success_rate_percent": 100.0
                * sum(bool(row.success) for row in selected)
                / len(selected),
                "mean_unaffected_disturbance_percent": 100.0
                * mean(row.unaffected_disturbance_ratio for row in selected),
            }
        )
    tradeoff.sort(
        key=lambda item: (
            -item["success_rate_percent"],
            item["mean_rule_change_ratio_percent"],
        )
    )
    return {
        "experiment": "exp3",
        "mode": "pilot",
        "raw_trial_count": len(rows),
        "aggregate_row_count": len(aggregate_rows),
        "topology_seed_count": len({row.seed for row in rows}),
        "error_count": sum(item.level == "ERROR" for item in findings),
        "warning_count": sum(item.level == "WARNING" for item in findings),
        "method_tradeoff": tradeoff,
        "findings": [asdict(item) for item in findings],
    }


def _format_optional(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def _exp4_pilot_report(rows, aggregate_rows, findings: Sequence[SanityFinding]) -> str:
    payload = _exp4_pilot_summary_payload(rows, aggregate_rows, findings)
    lines = [
        "# Exp.4 Pilot Summary",
        "",
        "> Pilot only — these values are for implementation and stress-range checks, not paper results.",
        "",
        f"- Raw trials: {payload['raw_trial_count']}",
        f"- Topology seeds: {payload['topology_seed_count']}",
        f"- Sanity errors: {payload['error_count']}",
        f"- Sanity warnings: {payload['warning_count']}",
        "",
        "## Failure Type Results",
        "",
        "| Failure Type | Method | Mean Successful Latency (ms) | Success Rate (%) | Rule Change Ratio (%) |",
        "|---|---|---:|---:|---:|",
    ]
    for item in payload["failure_type_results"]:
        lines.append(
            f"| {item['failure_type']} | {item['method_label']} "
            f"| {_format_optional(item['mean_recovery_latency_ms'])} "
            f"| {item['success_rate_percent']:.1f} "
            f"| {item['rule_change_ratio_percent']:.1f} |"
        )
    lines.extend(("", "## Capacity Stress", ""))
    for item in payload["capacity_stress"]:
        lines.append(
            f"- {item['method_label']}: "
            + ", ".join(
                f"{point['reduction_percent']:.0f}%={point['success_rate_percent']:.1f}%"
                for point in item["points"]
            )
        )
    lines.extend(("", "## Sanity Findings", ""))
    if findings:
        lines.extend(
            f"- [{item.level}] `{item.code}`: {item.message}"
            for item in findings
        )
    else:
        lines.append("- No findings.")
    lines.extend(
        (
            "",
            "## Outputs",
            "",
            "- Raw CSV: `results/raw/pilot/exp4/trials.csv`",
            "- Aggregate CSV: `results/aggregated/pilot/exp4/summary.csv`",
            "- Figure: `results/paper_figures/Fig4_Failure_Recovery.{pdf,png}`",
            "",
        )
    )
    return "\n".join(lines)


def _exp4_pilot_summary_payload(rows, aggregate_rows, findings) -> dict[str, object]:
    failure_results = []
    for failure_type in ("agent_failure", "link_failure", "capacity_degradation"):
        for method_id in ("proposed", "netkeeper", "cspf", "full_rebuild"):
            selected = [
                row
                for row in rows
                if row.series == "failure_type"
                and row.failure_type == failure_type
                and row.method_id == method_id
            ]
            latencies = [
                row.recovery_latency_ms
                for row in selected
                if row.recovery_latency_ms is not None
            ]
            failure_results.append(
                {
                    "failure_type": failure_type,
                    "method_id": method_id,
                    "method_label": METHODS[method_id].label,
                    "mean_recovery_latency_ms": (
                        mean(latencies) if latencies else None
                    ),
                    "success_rate_percent": 100.0
                    * sum(bool(row.success) for row in selected)
                    / len(selected),
                    "rule_change_ratio_percent": 100.0
                    * mean(row.rule_change_ratio for row in selected),
                }
            )
    stress = []
    for method_id in ("proposed", "netkeeper", "cspf", "full_rebuild"):
        points = []
        selected = [
            row
            for row in rows
            if row.series == "capacity_stress" and row.method_id == method_id
        ]
        for severity in sorted({row.failure_severity for row in selected}):
            point_rows = [row for row in selected if row.failure_severity == severity]
            points.append(
                {
                    "reduction_percent": 100.0 * severity,
                    "success_rate_percent": 100.0
                    * sum(bool(row.success) for row in point_rows)
                    / len(point_rows),
                }
            )
        stress.append(
            {
                "method_id": method_id,
                "method_label": METHODS[method_id].label,
                "points": points,
            }
        )
    return {
        "experiment": "exp4",
        "mode": "pilot",
        "raw_trial_count": len(rows),
        "aggregate_row_count": len(aggregate_rows),
        "topology_seed_count": len({row.seed for row in rows}),
        "error_count": sum(item.level == "ERROR" for item in findings),
        "warning_count": sum(item.level == "WARNING" for item in findings),
        "failure_type_results": failure_results,
        "capacity_stress": stress,
        "findings": [asdict(item) for item in findings],
    }


def _clear_pilot(output_root: Path, experiment: str) -> None:
    raw_dir = output_root / "raw" / "pilot" / experiment
    aggregate_dir = output_root / "aggregated" / "pilot" / experiment
    for directory in (raw_dir, aggregate_dir):
        if directory.exists():
            shutil.rmtree(directory)
    figure_name = {
        "exp1": "Fig1_Formation",
        "exp2": "Fig2_Cross_Layer_Coordination",
        "exp3": "Fig3_Business_Elasticity",
        "exp4": "Fig4_Failure_Recovery",
    }[experiment]
    for suffix in ("pdf", "png"):
        figure = output_root / "paper_figures" / f"{figure_name}.{suffix}"
        if figure.exists():
            figure.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run canonical paper pilot experiments")
    parser.add_argument("--experiments", nargs="+", default=("exp1",))
    parser.add_argument("--output-root", type=Path, default=Path("results"))
    parser.add_argument("--bootstrap-iterations", type=int, default=5000)
    parser.add_argument("--replace-pilot", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = asyncio.run(
        run_pilot(
            args.experiments,
            output_root=args.output_root,
            bootstrap_iterations=args.bootstrap_iterations,
            replace_existing=args.replace_pilot,
        )
    )
    print(
        f"pilot wrote {result.raw_trial_count} raw trials and "
        f"{result.aggregate_row_count} aggregate rows "
        f"(errors={result.error_count}, warnings={result.warning_count})"
    )
    if result.error_count:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
