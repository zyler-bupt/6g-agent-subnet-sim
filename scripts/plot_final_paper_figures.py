from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Iterable

from experiments.paper_protocol import EXPERIMENT_METHODS
from scripts.paper_style import (
    CI_ALPHA,
    METHOD_STYLES,
    apply_paper_style,
    method_line_kwargs,
    panel_label,
    style_axis,
)

import matplotlib.pyplot as plt


def plot_exp1(
    summary_csv: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    """Render the final two-panel formation figure from aggregated rows."""

    rows = _read_csv(Path(summary_csv))
    latency = [
        row
        for row in rows
        if row["experiment"] == "exp1"
        and row["series"] == "task_size"
        and row["metric"] == "formation_latency_ms"
    ]
    success = [
        row
        for row in rows
        if row["experiment"] == "exp1"
        and row["series"] == "state_churn"
        and row["metric"] == "success_rate_percent"
    ]
    _require_complete_methods(latency, "Exp.1 latency")
    _require_complete_methods(success, "Exp.1 churn success")

    apply_paper_style()
    figure, axes = plt.subplots(1, 2, figsize=(7.1, 2.25))
    _plot_lines(axes[0], latency)
    axes[0].set_xlabel("Number of Business Agents")
    axes[0].set_ylabel("Formation Latency (ms)")
    axes[0].set_ylim(bottom=0.0)
    axes[0].set_xticks(sorted({float(row["x_value"]) for row in latency}))
    style_axis(axes[0])
    panel_label(axes[0], "(a)")

    _plot_lines(axes[1], success)
    axes[1].set_xlabel("State Churn Probability (%)")
    axes[1].set_ylabel("Formation Success Rate (%)")
    axes[1].set_ylim(0.0, 105.0)
    axes[1].set_yticks((0, 20, 40, 60, 80, 100))
    axes[1].set_xticks(sorted({float(row["x_value"]) for row in success}))
    style_axis(axes[1])
    panel_label(axes[1], "(b)")

    handles, labels = axes[0].get_legend_handles_labels()
    figure.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.02),
        ncol=4,
        columnspacing=1.1,
        handlelength=2.2,
    )
    figure.subplots_adjust(left=0.083, right=0.992, bottom=0.22, top=0.79, wspace=0.27)

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    pdf = destination / "Fig1_Formation.pdf"
    png = destination / "Fig1_Formation.png"
    figure.savefig(
        pdf,
        format="pdf",
        metadata={
            "Title": "Initial Task Subnet Formation",
            "Subject": "Vector paper figure generated from canonical aggregate CSV",
        },
    )
    figure.savefig(png, format="png", dpi=300)
    plt.close(figure)
    return pdf, png


def _plot_lines(axis, rows: Iterable[dict[str, str]]) -> None:
    by_method: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_method.setdefault(row["method_id"], []).append(row)
    for method_id in EXPERIMENT_METHODS["exp1"]:
        selected = sorted(
            by_method[method_id],
            key=lambda row: float(row["x_value"]),
        )
        x_values = [float(row["x_value"]) for row in selected]
        means = [float(row["mean"]) for row in selected]
        lowers = [float(row["ci_lower"]) for row in selected]
        uppers = [float(row["ci_upper"]) for row in selected]
        line = axis.plot(x_values, means, **method_line_kwargs(method_id))[0]
        axis.fill_between(
            x_values,
            lowers,
            uppers,
            color=line.get_color(),
            alpha=CI_ALPHA,
            linewidth=0.0,
            zorder=1,
        )


def _require_complete_methods(rows: Iterable[dict[str, str]], context: str) -> None:
    materialized = list(rows)
    if not materialized:
        raise ValueError(f"no aggregate rows are available for {context}")
    methods = {row["method_id"] for row in materialized}
    expected = set(EXPERIMENT_METHODS["exp1"])
    if methods != expected:
        raise ValueError(
            f"{context} methods are incomplete: {sorted(methods)} != {sorted(expected)}"
        )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plot final composite paper figures")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--output-dir", default="results/paper_figures")
    parser.add_argument("--experiment", choices=("exp1",), default="exp1")
    return parser


def main() -> None:
    args = _parser().parse_args()
    outputs = plot_exp1(args.summary, args.output_dir)
    print("wrote " + ", ".join(str(path.resolve()) for path in outputs))


if __name__ == "__main__":
    main()
