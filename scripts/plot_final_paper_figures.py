from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.paper_protocol import EXPERIMENT_METHODS
from scripts.paper_style import (
    CI_ALPHA,
    METHOD_STYLES,
    apply_paper_style,
    method_bar_kwargs,
    method_line_kwargs,
    panel_label,
    style_axis,
)

import matplotlib.pyplot as plt
import numpy as np


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
    _require_complete_methods(latency, EXPERIMENT_METHODS["exp1"], "Exp.1 latency")
    _require_complete_methods(
        success,
        EXPERIMENT_METHODS["exp1"],
        "Exp.1 churn success",
    )

    apply_paper_style()
    figure, axes = plt.subplots(1, 2, figsize=(7.1, 2.25))
    _plot_lines(axes[0], latency, EXPERIMENT_METHODS["exp1"])
    axes[0].set_xlabel("Number of Business Agents")
    axes[0].set_ylabel("Formation Latency (ms)")
    axes[0].set_ylim(bottom=0.0)
    axes[0].set_xticks(sorted({float(row["x_value"]) for row in latency}))
    style_axis(axes[0])
    panel_label(axes[0], "(a)")

    _plot_lines(axes[1], success, EXPERIMENT_METHODS["exp1"])
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


def plot_exp3(
    summary_csv: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    """Render latency and the correctness--modification frontier for Exp.3."""

    rows = _read_csv(Path(summary_csv))
    latency = _metric_rows(rows, "exp3", "reconfiguration_latency_ms")
    rule_change = _metric_rows(rows, "exp3", "rule_change_ratio_percent")
    success = _metric_rows(rows, "exp3", "success_rate_percent")
    methods = EXPERIMENT_METHODS["exp3"]
    _require_complete_methods(latency, methods, "Exp.3 latency")
    _require_complete_methods(rule_change, methods, "Exp.3 rule change")
    _require_complete_methods(success, methods, "Exp.3 success")

    apply_paper_style()
    figure, axes = plt.subplots(1, 2, figsize=(7.1, 2.25))
    _plot_lines(axes[0], latency, methods)
    axes[0].set_xlabel("Affected Scope Ratio (%)")
    axes[0].set_ylabel("Reconfiguration Latency (ms)")
    axes[0].set_ylim(bottom=0.0)
    axes[0].set_xticks(sorted({float(row["x_value"]) for row in latency}))
    style_axis(axes[0])
    panel_label(axes[0], "(a)")

    rules_by_method = _rows_by_method(rule_change)
    success_by_method = _rows_by_method(success)
    for method_id in methods:
        method_rules = {
            float(row["x_value"]): row for row in rules_by_method[method_id]
        }
        method_success = {
            float(row["x_value"]): row for row in success_by_method[method_id]
        }
        scope_points = sorted(set(method_rules) & set(method_success))
        x_values = [float(method_rules[scope]["mean"]) for scope in scope_points]
        y_values = [float(method_success[scope]["mean"]) for scope in scope_points]
        line = axes[1].plot(
            x_values,
            y_values,
            **method_line_kwargs(method_id),
        )[0]
        x_lower = [
            x - float(method_rules[scope]["ci_lower"])
            for x, scope in zip(x_values, scope_points)
        ]
        x_upper = [
            float(method_rules[scope]["ci_upper"]) - x
            for x, scope in zip(x_values, scope_points)
        ]
        y_lower = [
            y - float(method_success[scope]["ci_lower"])
            for y, scope in zip(y_values, scope_points)
        ]
        y_upper = [
            float(method_success[scope]["ci_upper"]) - y
            for y, scope in zip(y_values, scope_points)
        ]
        axes[1].errorbar(
            x_values,
            y_values,
            xerr=(x_lower, x_upper),
            yerr=(y_lower, y_upper),
            fmt="none",
            ecolor=line.get_color(),
            elinewidth=0.7,
            capsize=1.8,
            alpha=0.45,
            zorder=2,
        )
    axes[1].set_xlabel("Rule Change Ratio (%)")
    axes[1].set_ylabel("Reconfiguration Success Rate (%)")
    axes[1].set_xlim(left=0.0)
    axes[1].set_ylim(0.0, 105.0)
    axes[1].set_yticks((0, 20, 40, 60, 80, 100))
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
    pdf = destination / "Fig3_Business_Elasticity.pdf"
    png = destination / "Fig3_Business_Elasticity.png"
    figure.savefig(
        pdf,
        format="pdf",
        metadata={
            "Title": "Business-Change-Driven Elastic Reconfiguration",
            "Subject": "Vector paper figure generated from canonical aggregate CSV",
        },
    )
    figure.savefig(png, format="png", dpi=300)
    plt.close(figure)
    return pdf, png


def plot_exp4(
    summary_csv: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    """Render failure latency/success bars and the capacity-stress curve."""

    rows = _read_csv(Path(summary_csv))
    methods = EXPERIMENT_METHODS["exp4"]
    latency = _metric_rows_for_series(
        rows,
        "exp4",
        "failure_type",
        "recovery_latency_ms",
    )
    success = _metric_rows_for_series(
        rows,
        "exp4",
        "failure_type",
        "success_rate_percent",
    )
    stress = _metric_rows_for_series(
        rows,
        "exp4",
        "capacity_stress",
        "success_rate_percent",
    )
    _require_complete_methods(success, methods, "Exp.4 recovery success")
    _require_complete_methods(stress, methods, "Exp.4 capacity stress")

    apply_paper_style()
    figure, axes = plt.subplots(1, 3, figsize=(7.1, 2.25))
    _plot_grouped_bars(
        axes[0],
        latency,
        methods,
        x_values=(0.0, 1.0, 2.0),
        missing_label="N/A",
    )
    axes[0].set_ylabel("Recovery Latency (ms)")
    axes[0].set_ylim(bottom=0.0)
    _failure_type_ticks(axes[0])
    style_axis(axes[0])
    panel_label(axes[0], "(a)")

    _plot_grouped_bars(
        axes[1],
        success,
        methods,
        x_values=(0.0, 1.0, 2.0),
    )
    axes[1].set_ylabel("Recovery Success Rate (%)")
    axes[1].set_ylim(0.0, 105.0)
    axes[1].set_yticks((0, 20, 40, 60, 80, 100))
    _failure_type_ticks(axes[1])
    style_axis(axes[1])
    panel_label(axes[1], "(b)")

    _plot_lines(axes[2], stress, methods)
    axes[2].set_xlabel("Capacity Reduction (%)")
    axes[2].set_ylabel("Recovery Success Rate (%)")
    axes[2].set_ylim(0.0, 105.0)
    axes[2].set_yticks((0, 20, 40, 60, 80, 100))
    axes[2].set_xticks(sorted({float(row["x_value"]) for row in stress}))
    style_axis(axes[2])
    panel_label(axes[2], "(c)")

    handles, labels = axes[1].get_legend_handles_labels()
    figure.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.02),
        ncol=4,
        columnspacing=0.9,
        handlelength=1.9,
    )
    figure.subplots_adjust(
        left=0.073,
        right=0.995,
        bottom=0.27,
        top=0.77,
        wspace=0.40,
    )
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    pdf = destination / "Fig4_Failure_Recovery.pdf"
    png = destination / "Fig4_Failure_Recovery.png"
    figure.savefig(
        pdf,
        format="pdf",
        metadata={
            "Title": "Failure-Driven Elastic Recovery",
            "Subject": "Vector paper figure generated from canonical aggregate CSV",
        },
    )
    figure.savefig(png, format="png", dpi=300)
    plt.close(figure)
    return pdf, png


def _plot_grouped_bars(
    axis,
    rows: Iterable[dict[str, str]],
    method_ids: tuple[str, ...],
    *,
    x_values: tuple[float, ...],
    missing_label: str | None = None,
) -> None:
    values = {
        (row["method_id"], float(row["x_value"])): row for row in rows
    }
    width = 0.19
    center = (len(method_ids) - 1) / 2.0
    for method_index, method_id in enumerate(method_ids):
        positions = np.asarray(x_values) + (method_index - center) * width
        selected = [values.get((method_id, x_value)) for x_value in x_values]
        means = np.asarray(
            [float(row["mean"]) if row is not None else np.nan for row in selected]
        )
        lower = np.asarray(
            [
                float(row["mean"]) - float(row["ci_lower"])
                if row is not None
                else 0.0
                for row in selected
            ]
        )
        upper = np.asarray(
            [
                float(row["ci_upper"]) - float(row["mean"])
                if row is not None
                else 0.0
                for row in selected
            ]
        )
        axis.bar(
            positions,
            means,
            width,
            yerr=(lower, upper),
            error_kw={"elinewidth": 0.7, "capsize": 1.5, "capthick": 0.7},
            **method_bar_kwargs(method_id),
        )
        if missing_label:
            for position, row in zip(positions, selected):
                if row is None:
                    axis.text(
                        position,
                        0.15,
                        missing_label,
                        rotation=90,
                        ha="center",
                        va="bottom",
                        fontsize=6.5,
                        color=METHOD_STYLES[method_id].color,
                    )


def _failure_type_ticks(axis) -> None:
    axis.set_xticks((0.0, 1.0, 2.0))
    axis.set_xticklabels(("Agent\nFailure", "Link\nFailure", "Capacity\n(30%)"))


def _plot_lines(
    axis,
    rows: Iterable[dict[str, str]],
    method_ids: Iterable[str],
) -> None:
    by_method: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_method.setdefault(row["method_id"], []).append(row)
    for method_id in method_ids:
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


def _require_complete_methods(
    rows: Iterable[dict[str, str]],
    method_ids: Iterable[str],
    context: str,
) -> None:
    materialized = list(rows)
    if not materialized:
        raise ValueError(f"no aggregate rows are available for {context}")
    methods = {row["method_id"] for row in materialized}
    expected = set(method_ids)
    if methods != expected:
        raise ValueError(
            f"{context} methods are incomplete: {sorted(methods)} != {sorted(expected)}"
        )


def _metric_rows(
    rows: Iterable[dict[str, str]],
    experiment: str,
    metric: str,
) -> list[dict[str, str]]:
    return [
        row
        for row in rows
        if row["experiment"] == experiment
        and row["series"] == "affected_scope"
        and row["metric"] == metric
    ]


def _metric_rows_for_series(
    rows: Iterable[dict[str, str]],
    experiment: str,
    series: str,
    metric: str,
) -> list[dict[str, str]]:
    return [
        row
        for row in rows
        if row["experiment"] == experiment
        and row["series"] == series
        and row["metric"] == metric
    ]


def _rows_by_method(
    rows: Iterable[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["method_id"], []).append(row)
    return grouped


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plot final composite paper figures")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--output-dir", default="results/paper_figures")
    parser.add_argument(
        "--experiment",
        choices=("exp1", "exp3", "exp4"),
        default="exp1",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    plotter = {
        "exp1": plot_exp1,
        "exp3": plot_exp3,
        "exp4": plot_exp4,
    }[args.experiment]
    outputs = plotter(args.summary, args.output_dir)
    print("wrote " + ", ".join(str(path.resolve()) for path in outputs))


if __name__ == "__main__":
    main()
