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
from experiments.paper_protocol import METHODS
from scripts.aggregate_results import aggregate_experiment
from scripts.plot_final_paper_figures import plot_exp1
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
    bootstrap_iterations: int = 5000,
    replace_existing: bool = False,
) -> PilotRunResult:
    selected = tuple(dict.fromkeys(experiments))
    unsupported = set(selected) - {"exp1"}
    if unsupported:
        raise ValueError(
            "pilot orchestration is not implemented yet for: "
            + ", ".join(sorted(unsupported))
        )
    if selected != ("exp1",):
        raise ValueError("this Phase-1 pilot checkpoint requires --experiments exp1")

    raw_path = output_root / "raw" / "pilot" / "exp1" / "trials.csv"
    aggregate_dir = output_root / "aggregated" / "pilot" / "exp1"
    summary_path = aggregate_dir / "summary.csv"
    figure_dir = output_root / "paper_figures"
    if raw_path.exists() and not replace_existing:
        raise FileExistsError(
            f"authoritative pilot already exists at {raw_path}; use --replace-pilot "
            "to rerun every method"
        )
    if replace_existing:
        _clear_exp1_pilot(output_root)

    rows = await run_exp1(
        "pilot",
        output_root,
        seeds=seeds,
        task_sizes=exp1_task_sizes,
        churn_points=exp1_churn_points,
    )
    aggregate_rows = aggregate_experiment(
        raw_path,
        summary_path,
        bootstrap_iterations=bootstrap_iterations,
    )
    findings = check_results(raw_path, experiment="exp1")
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
    plot_exp1(summary_path, figure_dir)
    report = _pilot_report(rows, aggregate_rows, findings)
    (aggregate_dir / "PILOT_SUMMARY.md").write_text(report, encoding="utf-8")
    with (aggregate_dir / "PILOT_SUMMARY.json").open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as handle:
        json.dump(
            _pilot_summary_payload(rows, aggregate_rows, findings),
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


def _pilot_report(rows, aggregate_rows, findings: Sequence[SanityFinding]) -> str:
    payload = _pilot_summary_payload(rows, aggregate_rows, findings)
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


def _pilot_summary_payload(rows, aggregate_rows, findings) -> dict[str, object]:
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


def _clear_exp1_pilot(output_root: Path) -> None:
    raw_dir = output_root / "raw" / "pilot" / "exp1"
    aggregate_dir = output_root / "aggregated" / "pilot" / "exp1"
    for directory in (raw_dir, aggregate_dir):
        if directory.exists():
            shutil.rmtree(directory)
    for suffix in ("pdf", "png"):
        figure = output_root / "paper_figures" / f"Fig1_Formation.{suffix}"
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
