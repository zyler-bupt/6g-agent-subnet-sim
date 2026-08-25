from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.paper_protocol import EXPERIMENT_METHODS, figure_min_topology_clusters
from scripts.paper_style import (
    CI_ALPHA,
    METHOD_STYLES,
    MethodStyle,
    apply_paper_style,
    method_bar_kwargs,
    method_line_kwargs,
    panel_label,
    style_axis,
)

import matplotlib.pyplot as plt
import numpy as np


METHOD_STYLES.setdefault(
    "sfc_reoptimization",
    MethodStyle("SFC Re-opt", "#f39c12", ":", "^", ".."),
)
METHOD_STYLES.setdefault(
    "weighted_sum",
    MethodStyle("Weighted-Sum", "#f39c12", ":", "^", ".."),
)
METHOD_STYLES.setdefault(
    "sfc_restoration",
    MethodStyle("SFC-Restore", "#f39c12", ":", "^", ".."),
)
METHOD_STYLES.setdefault(
    "te_reopt",
    MethodStyle("TE-Reopt", "#e74c3c", "--", "s", "//"),
)


def plot_exp1(
    summary_csv: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path, Path]:
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
        and row["series"] == "task_size"
        and row["metric"] == "success_rate_percent"
    ]
    methods = EXPERIMENT_METHODS["exp1"]
    latency = [row for row in latency if row["method_id"] in methods]
    success = [row for row in success if row["method_id"] in methods]
    _require_complete_methods(latency, methods, "Exp.1 latency")
    _require_complete_methods(
        success,
        methods,
        "Exp.1 task-size success",
    )

    apply_paper_style()
    figure, axes = plt.subplots(1, 2, figsize=(7.0, 2.5))
    _plot_lines(axes[0], latency, methods, shade_ci=True)
    axes[0].set_xlabel("Number of Business Agents")
    axes[0].set_ylabel("Formation Latency (ms, log scale)")
    axes[0].set_yscale("log")
    axes[0].set_ylim(70.0, 8000.0)
    axes[0].set_yticks((100.0, 300.0, 1000.0, 3000.0))
    axes[0].set_yticklabels(("100", "300", "1000", "3000"))
    axes[0].set_xticks(sorted({float(row["x_value"]) for row in latency}))
    style_axis(axes[0])
    panel_label(axes[0], "(a)")

    _plot_lines(axes[1], success, methods, shade_ci=True)
    axes[1].set_xlabel("Number of Business Agents")
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
    source = destination / "Fig1_Formation.csv"
    _write_figure_data(source, (("a", latency), ("b", success)))
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
    return pdf, png, source


def plot_exp2(
    summary_csv: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path, Path]:
    """Render the two main conditional coordination rates for Exp.2."""

    rows = _read_csv(Path(summary_csv))
    methods = EXPERIMENT_METHODS["exp2"]
    qos = _metric_rows_for_series(
        rows,
        "exp2",
        "conflict_density",
        "qos_satisfaction_rate_percent",
    )
    _require_complete_methods(qos, methods, "Exp.2 QoS satisfaction")
    available_density = sorted(
        {
            float(row["x_value"])
            for row in rows
            if row["experiment"] == "exp2"
            and row["series"] == "conflict_density"
        }
    )
    bar_density = 50.0 if 50.0 in available_density else max(available_density)
    high_conflict = [
        row
        for row in rows
        if row["experiment"] == "exp2"
        and row["series"] == "conflict_density"
        and float(row["x_value"]) == bar_density
        and row["metric"]
        in {"feasible_solution_rate_percent", "safe_rejection_rate_percent"}
    ]
    _require_complete_methods(high_conflict, methods, "Exp.2 conditional rates")
    metric_x = {
        "feasible_solution_rate_percent": 0.0,
        "safe_rejection_rate_percent": 1.0,
    }
    bar_rows = [
        {**row, "x_value": str(metric_x[row["metric"]])}
        for row in high_conflict
    ]

    apply_paper_style()
    figure, axes = plt.subplots(1, 2, figsize=(7.0, 2.5))
    _plot_lines(axes[0], qos, methods, shade_ci=False)
    axes[0].set_xlabel("Conflict Density (%)")
    axes[0].set_ylabel("QoS Satisfaction Rate (%)")
    axes[0].set_ylim(0.0, 105.0)
    axes[0].set_yticks((0, 20, 40, 60, 80, 100))
    axes[0].set_xticks(sorted({float(row["x_value"]) for row in qos}))
    style_axis(axes[0])
    panel_label(axes[0], "(a)")

    _plot_grouped_bars(axes[1], bar_rows, methods, x_values=(0.0, 1.0))
    axes[1].set_xticks((0.0, 1.0))
    axes[1].set_xticklabels(("Feasible\nSolution", "Safe\nRejection"))
    axes[1].set_xlabel(f"Conditional Metric at {bar_density:g}% Conflict")
    axes[1].set_ylabel("Rate (%)")
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
    pdf = destination / "Fig2_CrossLayer.pdf"
    png = destination / "Fig2_CrossLayer.png"
    source = destination / "Fig2_CrossLayer.csv"
    _write_figure_data(source, (("a", qos), ("b", high_conflict)))
    figure.savefig(
        pdf,
        format="pdf",
        metadata={
            "Title": "Cross-Layer Conflict Resolution",
            "Subject": "Vector paper figure generated from conditional aggregate CSV",
        },
    )
    figure.savefig(png, format="png", dpi=300)
    plt.close(figure)
    return pdf, png, source


def plot_exp3(
    summary_csv: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path, Path]:
    """Render latency and modification scope over affected Agent count."""

    rows = _read_csv(Path(summary_csv))
    latency = _metric_rows_for_series(
        rows, "exp3", "affected_agents", "reconfiguration_latency_ms"
    )
    rule_change = _metric_rows_for_series(
        rows, "exp3", "affected_agents", "rule_change_ratio_percent"
    )
    # Exact dependency-closure sizes are observed rather than manufactured.
    # Consequently, the tail can be sparse.  Keep every value in raw and
    # aggregate CSVs, but only draw points with enough independent topology
    # clusters for an interpretable cluster-bootstrap interval.
    latency = _well_supported_exp3_rows(latency)
    rule_change = _well_supported_exp3_rows(rule_change)
    methods = EXPERIMENT_METHODS["exp3"]
    _require_complete_methods(latency, methods, "Exp.3 latency")
    _require_complete_methods(rule_change, methods, "Exp.3 rule change")

    apply_paper_style()
    figure, axes = plt.subplots(1, 2, figsize=(7.0, 2.5))
    _plot_lines(axes[0], latency, methods, shade_ci=True)
    axes[0].set_xlabel("Number of Affected Agents")
    axes[0].set_ylabel("Reconfiguration Latency (ms)")
    axes[0].set_ylim(bottom=0.0)
    axes[0].set_xticks(sorted({float(row["x_value"]) for row in latency}))
    style_axis(axes[0])
    panel_label(axes[0], "(a)")

    _plot_lines(axes[1], rule_change, methods, shade_ci=True)
    axes[1].set_xlabel("Number of Affected Agents")
    axes[1].set_ylabel("Changed Rule Ratio (%)")
    axes[1].set_ylim(bottom=0.0)
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
    pdf = destination / "Fig3_Elasticity.pdf"
    png = destination / "Fig3_Elasticity.png"
    source = destination / "Fig3_Elasticity.csv"
    _write_figure_data(source, (("a", latency), ("b", rule_change)))
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
    return pdf, png, source


def _well_supported_exp3_rows(
    rows: Iterable[dict[str, str]],
) -> list[dict[str, str]]:
    selected = []
    for row in rows:
        threshold = figure_min_topology_clusters(row.get("mode", "paper"))
        if int(float(row.get("cluster_count", "0") or 0)) >= threshold:
            selected.append(row)
    return selected


def plot_exp4(
    summary_csv: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path, Path]:
    """Render recovery latency and physical modification-scope bars."""

    rows = _read_csv(Path(summary_csv))
    methods = EXPERIMENT_METHODS["exp4"]
    latency = _metric_rows_for_series(
        rows,
        "exp4",
        "failure_type",
        "recovery_latency_ms",
    )
    modification_scope = _metric_rows_for_series(
        rows,
        "exp4",
        "failure_type",
        "modification_scope_ratio_percent",
    )
    _require_complete_methods(latency, methods, "Exp.4 recovery latency")
    _require_complete_methods(
        modification_scope, methods, "Exp.4 modification scope"
    )

    apply_paper_style()
    figure, axes = plt.subplots(1, 2, figsize=(7.0, 2.5))
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
        modification_scope,
        methods,
        x_values=(0.0, 1.0, 2.0),
        missing_label="N/A",
    )
    axes[1].set_ylabel("Modification Scope (%)")
    axes[1].set_ylim(bottom=0.0)
    _failure_type_ticks(axes[1])
    style_axis(axes[1])
    panel_label(axes[1], "(b)")

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
    pdf = destination / "Fig4_Recovery.pdf"
    png = destination / "Fig4_Recovery.png"
    source = destination / "Fig4_Recovery.csv"
    _write_figure_data(
        source,
        (("a", latency), ("b", modification_scope)),
    )
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
    return pdf, png, source


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
    *,
    shade_ci: bool,
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
        if shade_ci:
            axis.fill_between(
                x_values,
                lowers,
                uppers,
                color=line.get_color(),
                alpha=CI_ALPHA,
                linewidth=0.0,
                zorder=1,
            )
        else:
            axis.errorbar(
                x_values,
                means,
                yerr=(
                    [mean - lower for mean, lower in zip(means, lowers)],
                    [upper - mean for mean, upper in zip(means, uppers)],
                ),
                fmt="none",
                ecolor=line.get_color(),
                elinewidth=0.7,
                capsize=1.8,
                alpha=0.55,
                zorder=2,
            )


def _write_figure_data(
    path: Path,
    panels: Iterable[tuple[str, Iterable[dict[str, str]]]],
) -> None:
    materialized = [
        {"panel": panel, **row}
        for panel, rows in panels
        for row in rows
    ]
    if not materialized:
        raise ValueError("figure source data must not be empty")
    fieldnames = ["panel"] + sorted(
        {key for row in materialized for key in row if key != "panel"}
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(materialized)


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
        choices=("exp1", "exp2", "exp3", "exp4"),
        default="exp1",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    plotter = {
        "exp1": plot_exp1,
        "exp2": plot_exp2,
        "exp3": plot_exp3,
        "exp4": plot_exp4,
    }[args.experiment]
    outputs = plotter(args.summary, args.output_dir)
    print("wrote " + ", ".join(str(path.resolve()) for path in outputs))


if __name__ == "__main__":
    main()
