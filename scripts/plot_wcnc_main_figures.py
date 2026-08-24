from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

import yaml

os.environ.setdefault("MPLCONFIGDIR", "/tmp/6g-agent-subnet-wcnc-matplotlib")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    from scripts.paper_style import (
        METHOD_STYLES,
        apply_paper_style,
        method_bar_kwargs,
        method_line_kwargs,
        panel_label,
        style_axis,
    )
except ModuleNotFoundError:  # direct execution from the scripts directory
    from paper_style import (
        METHOD_STYLES,
        apply_paper_style,
        method_bar_kwargs,
        method_line_kwargs,
        panel_label,
        style_axis,
    )


EXP2_METHOD_ORDER = (
    "ours",
    "alc",
    "layer_wise_independent",
    "no_global_verification",
)
PAIR_METHOD_ORDER = ("ours", "full_reconfiguration")
EXP4_METHOD_ORDER = ("ours", "full_reconfiguration", "cspf")
EXP3_RAW_TO_FINAL = {
    "proposed": "ours",
    "full_rebuild": "full_reconfiguration",
}
EXP4_RAW_TO_FINAL = {
    "proposed": "ours",
    "full_rebuild": "full_reconfiguration",
    "cspf": "cspf",
}
FAILURE_ORDER = (
    "Agent Failure",
    "Link Failure",
    "Physical Capacity Failure",
)
FAILURE_TICK_LABELS = {
    "Agent Failure": "Agent\nFailure",
    "Link Failure": "Link\nFailure",
    "Physical Capacity Failure": "Physical Capacity\nFailure",
}
GROUND_TRUTH_ORDER = (
    "NO_CONFLICT",
    "RESOLVABLE_CONFLICT",
    "UNRESOLVABLE_CONFLICT",
)


def generate(
    *,
    exp1_dir: Path,
    exp2_dir: Path,
    exp3_dir: Path,
    exp4_dir: Path,
    output_dir: Path,
) -> None:
    apply_paper_style()
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    exp1_raw_path = exp1_dir / "raw" / "runs.csv"
    exp2_raw_path = exp2_dir / "raw" / "runs.csv"
    exp3_raw_path = exp3_dir / "raw" / "runs.csv"
    exp4_raw_path = exp4_dir / "raw" / "runs.csv"

    fig1 = build_fig1_data(exp1_raw_path)
    fig2 = build_fig2_data(exp2_raw_path)
    fig3a, fig3b = build_fig3_data(exp3_raw_path)
    fig4a, fig4b = build_fig4_data(exp4_raw_path)

    _write_csv(data_dir / "fig1.csv", fig1)
    _write_csv(data_dir / "fig2.csv", fig2)
    _write_csv(data_dir / "fig3a.csv", fig3a)
    _write_csv(data_dir / "fig3b.csv", fig3b)
    _write_csv(data_dir / "fig4a.csv", fig4a)
    _write_csv(data_dir / "fig4b.csv", fig4b)

    _plot_fig1(fig1, output_dir)
    _plot_fig2(fig2, output_dir)
    _plot_fig3(fig3a, fig3b, output_dir)
    _plot_fig4(fig4a, fig4b, output_dir)
    _write_report(
        exp1_dir=exp1_dir,
        exp2_dir=exp2_dir,
        exp3_dir=exp3_dir,
        exp4_dir=exp4_dir,
        output_dir=output_dir,
        fig1=fig1,
        fig2=fig2,
        fig3a=fig3a,
        fig3b=fig3b,
        fig4a=fig4a,
        fig4b=fig4b,
    )


def build_fig1_data(raw_path: Path) -> list[dict[str, object]]:
    raw = _read_csv(raw_path)
    groups: dict[int, list[dict[str, str]]] = {}
    for row in raw:
        groups.setdefault(int(row["num_agents"]), []).append(row)
    output: list[dict[str, object]] = []
    for num_agents, rows in sorted(groups.items()):
        successful = [row for row in rows if _bool(row["success"])]
        if not successful:
            raise ValueError(f"Fig. 1 has no successful measured runs at {num_agents} Agents")
        values = [float(row["verified_formation_latency_s"]) for row in successful]
        mean, lower, upper, half = _continuous_interval(values)
        retry_runs = sum(int(row["retry_count"]) > 0 for row in rows)
        output.append(
            {
                "num_agents": num_agents,
                "method": METHOD_STYLES["ours"].label,
                "runs_total": len(rows),
                "runs_successful": len(successful),
                "formation_success_rate_percent": 100.0 * len(successful) / len(rows),
                "runs_with_retry": retry_runs,
                "retry_rate_percent": 100.0 * retry_runs / len(rows),
                "mean_s": mean,
                "ci95_s": half,
                "lower95_s": lower,
                "upper95_s": upper,
            }
        )
    return output


def build_fig2_data(raw_path: Path) -> list[dict[str, object]]:
    raw = _read_csv(raw_path)
    groups: dict[tuple[float, str], list[dict[str, str]]] = {}
    unique_scenarios: dict[tuple[float, int], dict[str, str]] = {}
    for row in raw:
        gamma = float(row["demand_to_capacity_ratio"])
        method = str(row["method"])
        groups.setdefault((gamma, method), []).append(row)
        unique_scenarios.setdefault((gamma, int(row["seed"])), row)
    unresolvable = Counter(
        gamma
        for (gamma, _seed), row in unique_scenarios.items()
        if row["conflict_class"] == "UNRESOLVABLE_CONFLICT"
    )
    scenarios_per_gamma = Counter(gamma for gamma, _seed in unique_scenarios)
    output: list[dict[str, object]] = []
    for gamma in sorted({key[0] for key in groups}):
        for method in EXP2_METHOD_ORDER:
            rows = groups[(gamma, method)]
            successes = sum(_bool(row["qos_satisfied"]) for row in rows)
            mean, lower, upper, half = _wilson_interval(successes, len(rows))
            output.append(
                {
                    "demand_to_capacity_ratio": gamma,
                    "method": METHOD_STYLES[method].label,
                    "samples": len(rows),
                    "satisfied_scenarios": successes,
                    "mean_percent": 100.0 * mean,
                    "ci95_percent": 100.0 * half,
                    "lower95_percent": 100.0 * lower,
                    "upper95_percent": 100.0 * upper,
                    "unresolvable_scenarios": unresolvable[gamma],
                    "unresolvable_ratio_percent": (
                        100.0 * unresolvable[gamma] / scenarios_per_gamma[gamma]
                    ),
                }
            )
    return output


def build_fig3_data(
    raw_path: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    raw = _read_csv(raw_path)
    latency_groups: dict[tuple[int, str], list[float]] = {}
    change_groups: dict[tuple[float, str], list[float]] = {}
    for row in raw:
        if row["method"] not in EXP3_RAW_TO_FINAL or not _bool(row["success"]):
            continue
        method = EXP3_RAW_TO_FINAL[row["method"]]
        if row["scenario"].startswith("agent_count:"):
            latency_groups.setdefault(
                (int(row["num_agents_before"]), method),
                [],
            ).append(float(row["elastic_latency_ms"]))
        elif row["scenario"].startswith("removal_ratio:"):
            change_groups.setdefault(
                (100.0 * float(row["agent_removal_ratio"]), method),
                [],
            ).append(100.0 * float(row["changed_rules_ratio"]))
    fig3a = _continuous_group_rows(latency_groups, "num_agents", scale=1.0)
    fig3b = _continuous_group_rows(
        change_groups,
        "removed_agents_percent",
        scale=1.0,
    )
    return fig3a, fig3b


def build_fig4_data(
    raw_path: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    raw = _read_csv(raw_path)
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in raw:
        family = _failure_family(row["fault_type"])
        method = EXP4_RAW_TO_FINAL[row["method"]]
        groups.setdefault((family, method), []).append(row)
    success_rows: list[dict[str, object]] = []
    change_rows: list[dict[str, object]] = []
    for family in FAILURE_ORDER:
        for method in EXP4_METHOD_ORDER:
            rows = groups[(family, method)]
            successes = sum(_bool(row["recovery_success"]) for row in rows)
            mean, lower, upper, half = _wilson_interval(successes, len(rows))
            success_rows.append(
                {
                    "failure_type": family,
                    "method": METHOD_STYLES[method].label,
                    "samples": len(rows),
                    "successful_recoveries": successes,
                    "mean_percent": 100.0 * mean,
                    "ci95_percent": 100.0 * half,
                    "lower95_percent": 100.0 * lower,
                    "upper95_percent": 100.0 * upper,
                }
            )
            if method in PAIR_METHOD_ORDER:
                values = [100.0 * float(row["rule_change_ratio"]) for row in rows]
                c_mean, c_lower, c_upper, c_half = _continuous_interval(values)
                change_rows.append(
                    {
                        "failure_type": family,
                        "method": METHOD_STYLES[method].label,
                        "samples": len(rows),
                        "mean_percent": c_mean,
                        "ci95_percent": c_half,
                        "lower95_percent": max(0.0, c_lower),
                        "upper95_percent": c_upper,
                    }
                )
    return success_rows, change_rows


def _continuous_group_rows(
    groups: dict[tuple[object, str], list[float]],
    x_name: str,
    *,
    scale: float,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for x_value in sorted({key[0] for key in groups}):
        for method in PAIR_METHOD_ORDER:
            values = groups[(x_value, method)]
            scaled = [scale * value for value in values]
            mean, lower, upper, half = _continuous_interval(scaled)
            output.append(
                {
                    x_name: x_value,
                    "method": METHOD_STYLES[method].label,
                    "samples": len(scaled),
                    "mean": mean,
                    "ci95": half,
                    "lower95": max(0.0, lower),
                    "upper95": upper,
                }
            )
    return output


def _plot_fig1(rows: Sequence[dict[str, object]], output_dir: Path) -> None:
    fig, axis = plt.subplots(figsize=(3.5, 2.5))
    x = [float(row["num_agents"]) for row in rows]
    mean = [float(row["mean_s"]) for row in rows]
    lower = [float(row["lower95_s"]) for row in rows]
    upper = [float(row["upper95_s"]) for row in rows]
    axis.fill_between(
        x,
        lower,
        upper,
        color=METHOD_STYLES["ours"].color,
        alpha=0.17,
        linewidth=0.0,
        zorder=1,
    )
    axis.plot(x, mean, **method_line_kwargs("ours"))
    axis.set_xlabel("Number of Business Agents")
    axis.set_ylabel("Verified Formation Latency (s)")
    axis.set_xticks(x)
    axis.set_ylim(bottom=0.0)
    style_axis(axis)
    axis.legend(loc="upper left")
    fig.tight_layout()
    _save(fig, output_dir / "Fig1_Verified_Formation")


def _plot_fig2(rows: Sequence[dict[str, object]], output_dir: Path) -> None:
    fig, axis = plt.subplots(figsize=(4.8, 2.65))
    for method in EXP2_METHOD_ORDER:
        label = METHOD_STYLES[method].label
        values = sorted(
            (row for row in rows if row["method"] == label),
            key=lambda row: float(row["demand_to_capacity_ratio"]),
        )
        x = [float(row["demand_to_capacity_ratio"]) for row in values]
        mean = [float(row["mean_percent"]) for row in values]
        lower = [float(row["lower95_percent"]) for row in values]
        upper = [float(row["upper95_percent"]) for row in values]
        axis.fill_between(
            x,
            lower,
            upper,
            color=METHOD_STYLES[method].color,
            alpha=0.11,
            linewidth=0.0,
            zorder=1,
        )
        axis.plot(x, mean, **method_line_kwargs(method))
    axis.set_xlabel("Demand-to-Capacity Ratio")
    axis.set_ylabel("Task Satisfaction Rate (%)")
    axis.set_xticks(sorted({float(row["demand_to_capacity_ratio"]) for row in rows}))
    axis.set_ylim(0.0, 100.0)
    axis.set_yticks((0, 20, 40, 60, 80, 100))
    style_axis(axis)
    axis.legend(
        loc="lower center",
        bbox_to_anchor=(0.5, 1.01),
        ncol=2,
        columnspacing=1.0,
        handlelength=2.6,
    )
    fig.tight_layout()
    _save(fig, output_dir / "Fig2_Cross_Layer_Conflict")


def _plot_fig3(
    latency_rows: Sequence[dict[str, object]],
    change_rows: Sequence[dict[str, object]],
    output_dir: Path,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(7.16, 2.75))
    for method in PAIR_METHOD_ORDER:
        label = METHOD_STYLES[method].label
        latency = sorted(
            (row for row in latency_rows if row["method"] == label),
            key=lambda row: float(row["num_agents"]),
        )
        _line_with_band(
            axes[0],
            latency,
            x_key="num_agents",
            mean_key="mean",
            lower_key="lower95",
            upper_key="upper95",
            method=method,
            alpha=0.14,
        )
        changes = sorted(
            (row for row in change_rows if row["method"] == label),
            key=lambda row: float(row["removed_agents_percent"]),
        )
        _line_with_band(
            axes[1],
            changes,
            x_key="removed_agents_percent",
            mean_key="mean",
            lower_key="lower95",
            upper_key="upper95",
            method=method,
            alpha=0.14,
        )
    axes[0].set_xlabel("Number of Business Agents")
    axes[0].set_ylabel("Reconfiguration Latency (ms)")
    axes[0].set_xticks(sorted({float(row["num_agents"]) for row in latency_rows}))
    axes[0].set_ylim(bottom=0.0)
    axes[1].set_xlabel("Removed Agents (%)")
    axes[1].set_ylabel("Rule Change Ratio (%)")
    axes[1].set_xticks(
        sorted({float(row["removed_agents_percent"]) for row in change_rows})
    )
    axes[1].set_ylim(
        0.0,
        _percentage_upper_bound(
            max(float(row["upper95"]) for row in change_rows)
        ),
    )
    for axis in axes:
        style_axis(axis)
    panel_label(axes[0], "(a)")
    panel_label(axes[1], "(b)")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.01),
        ncol=2,
        handlelength=2.8,
    )
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.91), w_pad=1.8)
    _save(fig, output_dir / "Fig3_Business_Reconfiguration")


def _plot_fig4(
    success_rows: Sequence[dict[str, object]],
    change_rows: Sequence[dict[str, object]],
    output_dir: Path,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(7.16, 2.8))
    base_x = list(range(len(FAILURE_ORDER)))
    width = 0.23
    for method_index, method in enumerate(EXP4_METHOD_ORDER):
        label = METHOD_STYLES[method].label
        values = [
            next(
                row
                for row in success_rows
                if row["failure_type"] == family and row["method"] == label
            )
            for family in FAILURE_ORDER
        ]
        positions = [
            value + (method_index - 1) * width for value in base_x
        ]
        means = [float(row["mean_percent"]) for row in values]
        lower_error = [
            mean - float(row["lower95_percent"])
            for mean, row in zip(means, values)
        ]
        upper_error = [
            float(row["upper95_percent"]) - mean
            for mean, row in zip(means, values)
        ]
        axes[0].bar(
            positions,
            means,
            width=width,
            yerr=[lower_error, upper_error],
            capsize=1.8,
            error_kw={"elinewidth": 0.7, "capthick": 0.7},
            **method_bar_kwargs(method),
        )
    width_pair = 0.31
    for method_index, method in enumerate(PAIR_METHOD_ORDER):
        label = METHOD_STYLES[method].label
        values = [
            next(
                row
                for row in change_rows
                if row["failure_type"] == family and row["method"] == label
            )
            for family in FAILURE_ORDER
        ]
        positions = [
            value + (method_index - 0.5) * width_pair for value in base_x
        ]
        means = [float(row["mean_percent"]) for row in values]
        lower_error = [
            mean - float(row["lower95_percent"])
            for mean, row in zip(means, values)
        ]
        upper_error = [
            float(row["upper95_percent"]) - mean
            for mean, row in zip(means, values)
        ]
        axes[1].bar(
            positions,
            means,
            width=width_pair,
            yerr=[lower_error, upper_error],
            capsize=1.8,
            error_kw={"elinewidth": 0.7, "capthick": 0.7},
            **method_bar_kwargs(method),
        )
    axes[0].set_ylabel("Recovery Success Rate (%)")
    axes[0].set_ylim(0.0, 100.0)
    axes[0].set_yticks((0, 20, 40, 60, 80, 100))
    axes[1].set_ylabel("Rule Change Ratio (%)")
    axes[1].set_ylim(
        0.0,
        _percentage_upper_bound(
            max(float(row["upper95_percent"]) for row in change_rows)
        ),
    )
    for axis in axes:
        axis.set_xticks(
            base_x,
            [FAILURE_TICK_LABELS[family] for family in FAILURE_ORDER],
        )
        style_axis(axis)
    panel_label(axes[0], "(a)")
    panel_label(axes[1], "(b)")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.01),
        ncol=3,
        handlelength=2.5,
    )
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.91), w_pad=1.8)
    _save(fig, output_dir / "Fig4_Failure_Recovery")


def _line_with_band(
    axis,
    rows: Sequence[dict[str, object]],
    *,
    x_key: str,
    mean_key: str,
    lower_key: str,
    upper_key: str,
    method: str,
    alpha: float,
) -> None:
    x = [float(row[x_key]) for row in rows]
    axis.fill_between(
        x,
        [float(row[lower_key]) for row in rows],
        [float(row[upper_key]) for row in rows],
        color=METHOD_STYLES[method].color,
        alpha=alpha,
        linewidth=0.0,
        zorder=1,
    )
    axis.plot(
        x,
        [float(row[mean_key]) for row in rows],
        **method_line_kwargs(method),
    )


def _write_report(
    *,
    exp1_dir: Path,
    exp2_dir: Path,
    exp3_dir: Path,
    exp4_dir: Path,
    output_dir: Path,
    fig1: Sequence[dict[str, object]],
    fig2: Sequence[dict[str, object]],
    fig3a: Sequence[dict[str, object]],
    fig3b: Sequence[dict[str, object]],
    fig4a: Sequence[dict[str, object]],
    fig4b: Sequence[dict[str, object]],
) -> None:
    exp1_raw_path = exp1_dir / "raw" / "runs.csv"
    exp2_raw_path = exp2_dir / "raw" / "runs.csv"
    exp3_raw_path = exp3_dir / "raw" / "runs.csv"
    exp4_raw_path = exp4_dir / "raw" / "runs.csv"
    exp1_raw = _read_csv(exp1_raw_path)
    exp2_raw = _read_csv(exp2_raw_path)
    exp4_raw = _read_csv(exp4_raw_path)
    scope = json.loads(
        (exp1_dir / "raw" / "measurement_scope.json").read_text(encoding="utf-8")
    )
    exp1_config = _load_yaml(Path("configs/exp1_netns_verified_formation.yaml"))
    exp2_config = _load_yaml(Path("configs/exp2_demand_capacity_ratio.yaml"))
    exp4_config = _load_yaml(
        Path("configs/exp4_cspf_failure_reconfiguration.yaml")
    )

    unique_exp2 = {
        (float(row["demand_to_capacity_ratio"]), int(row["seed"])): row
        for row in exp2_raw
    }
    class_counts = Counter(
        (gamma, row["conflict_class"])
        for (gamma, _seed), row in unique_exp2.items()
    )
    gamma_values = sorted({gamma for gamma, _seed in unique_exp2})
    highest_gamma = max(gamma_values)
    highest_unresolvable = class_counts[
        (highest_gamma, "UNRESOLVABLE_CONFLICT")
    ]
    scenarios_per_gamma = len(
        {seed for gamma, seed in unique_exp2 if gamma == highest_gamma}
    )

    pilot_statement = (
        "The pilot-only fully infeasible point was excluded before the main run."
    )
    pilot_path = Path(exp2_config["experiment"]["pilot_source"]) / "raw" / "runs.csv"
    if pilot_path.exists():
        pilot_rows = _read_csv(pilot_path)
        pilot_unique = {
            (float(row["demand_to_capacity_ratio"]), int(row["seed"])): row
            for row in pilot_rows
        }
        pilot_max = max(gamma for gamma, _seed in pilot_unique)
        pilot_at_max = [
            row for (gamma, _seed), row in pilot_unique.items() if gamma == pilot_max
        ]
        pilot_unresolvable = sum(
            row["conflict_class"] == "UNRESOLVABLE_CONFLICT"
            for row in pilot_at_max
        )
        pilot_statement = (
            f"The frozen pilot evaluated gamma={pilot_max:g}; "
            f"{pilot_unresolvable}/{len(pilot_at_max)} scenarios were intrinsically "
            "infeasible. That fully infeasible pilot-only region was excluded "
            "before the main run, whose transition-region endpoint is "
            f"gamma={highest_gamma:g}."
        )

    physical_rows = [
        row
        for row in exp4_raw
        if row["fault_type"] == "PHYSICAL_CAPACITY_DROP"
    ]
    physical_unique = {
        (row["scenario"], int(row["seed"])): row for row in physical_rows
    }
    minimum_physical_margin = min(
        float(row["pre_recovery_violation_margin_mbps"])
        for row in physical_unique.values()
    )
    physical_proof_count = sum(
        _bool(row["fault_was_disruptive"])
        and _bool(row["requirement_violated_before_recovery"])
        and float(row["post_failure_capacity_mbps"])
        < float(row["pre_recovery_requirement_mbps"])
        for row in physical_unique.values()
    )
    exp4_pairs = {
        (row["scenario"], int(row["seed"])) for row in exp4_raw
    }

    hashes = {
        "Fig. 1 raw runs": _sha256(exp1_raw_path),
        "Fig. 2 raw runs": _sha256(exp2_raw_path),
        "Fig. 3 raw runs": _sha256(exp3_raw_path),
        "Fig. 4 raw runs": _sha256(exp4_raw_path),
        "Fig. 3 events": _sha256(exp3_dir / "raw" / "events.jsonl"),
        "Fig. 3 probes": _sha256(exp3_dir / "raw" / "probe_samples.csv"),
        "Fig. 3 scenarios": _sha256(
            exp3_dir / "raw" / "scenario_snapshots.json"
        ),
    }
    code = _md_code
    lines = [
        "# WCNC Final Experiment Report",
        "",
        "This report is generated from raw artifacts by the same script that "
        "generates the plotting CSVs. No CSV value is edited by hand, no seed "
        "is selected or removed, no result is smoothed, and no random value is "
        "added to measured latency.",
        "",
        "## Reproducible commands",
        "",
        _fence("bash"),
        ".venv/bin/python -m experiments.exp1_netns_verified_formation "
        "--config configs/exp1_netns_verified_formation.yaml "
        "--output-dir results/exp1_wcnc_final --seeds 0:29",
        ".venv/bin/python scripts/aggregate_exp1_netns.py",
        ".venv/bin/python -m experiments.exp2_demand_capacity_ratio "
        "--config configs/exp2_demand_capacity_ratio.yaml "
        "--methods ours,alc,layer_wise_independent,no_global_verification "
        "--seeds 0:99 --output-dir results/exp2_demand_capacity",
        ".venv/bin/python scripts/aggregate_exp2_demand_capacity.py",
        ".venv/bin/python -m experiments.exp4_failure_reconfiguration "
        "--config configs/exp4_cspf_failure_reconfiguration.yaml "
        "--methods proposed,full_rebuild,cspf --seeds 0:29 "
        "--output-dir results/exp4_cspf_final --skip-timeline",
        ".venv/bin/python scripts/audit_exp4_cspf.py",
        ".venv/bin/python -m scripts.plot_wcnc_main_figures",
        ".venv/bin/python -m scripts.audit_wcnc_main",
        _fence(),
        "",
        "The existing Exp. 3 raw artifacts are inputs only and are never "
        "regenerated by this workflow.",
        "",
        "## Fig. 1 — Verified Task Subnet Formation",
        "",
        f"- Measurement mode: {code(scope['result_mode'])}.",
        f"- Namespace/veth mechanism: {scope['namespace_mechanism']}; each Agent "
        "and gateway uses a real process-owned Linux network namespace joined "
        "by veth links.",
        f"- Actual ping command: {code(scope['ping_command_template'])}.",
        f"- Ping retry policy: {scope['ping_retry_policy']}; the same failed "
        f"business edges are retried after {scope['retry_backoff_ms']} ms.",
        f"- Actual iPerf3 server command: "
        f"{code(scope['iperf3_server_command_template'])}.",
        f"- Actual iPerf3 client command: "
        f"{code(scope['iperf3_client_command_template'])}.",
        f"- iPerf3 duration: "
        f"{exp1_config['verification']['iperf3']['duration_s']} s for every flow "
        "and every experiment point.",
        f"- iPerf3 retry policy: {scope['iperf3_retry_policy']}; the same failed "
        f"or sub-threshold flows are retried after {scope['retry_backoff_ms']} ms.",
        f"- netem randomized ranges: base delay "
        f"{exp1_config['traffic_control']['netem']['base_delay_ms']} ms, jitter "
        f"{exp1_config['traffic_control']['netem']['jitter_ms']} ms, packet loss "
        f"{exp1_config['traffic_control']['netem']['packet_loss_percent']}%, and "
        f"queue limit {exp1_config['traffic_control']['netem']['queue_limit_packets']} "
        "packets.",
        f"- HTB available-bandwidth range: "
        f"{exp1_config['traffic_control']['htb']['bandwidth_mbps']} Mbps.",
        f"- Background traffic: probability "
        f"{exp1_config['traffic_control']['background_traffic']['enabled_probability']:.2f}, "
        f"utilization range "
        f"{exp1_config['traffic_control']['background_traffic']['utilization_range']}, "
        f"UDP duration "
        f"{exp1_config['traffic_control']['background_traffic']['duration_s']} s.",
        f"- Seed policy: all consecutive seeds 0–29, fixed in advance; "
        f"{len({int(row['seed']) for row in exp1_raw})} seeds are retained.",
        "- Formation latency is T_form = t_final_verified - t_task_received and "
        "includes mapping, Linux traffic-control installation, route/rule "
        "compilation and installation, activation, ping, iPerf3, and any real "
        "retry/backoff. Parallel flow verification is measured by wall-clock "
        "time from verification start to the last completed flow; individual "
        "iPerf3 durations are not summed.",
        "",
        "| Business Agents | Runs | Success rate (%) | Retry rate (%) | Mean T_form (s) | 95% CI (s) |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in fig1:
        lines.append(
            f"| {row['num_agents']} | {row['runs_total']} | "
            f"{row['formation_success_rate_percent']:.1f} | "
            f"{row['retry_rate_percent']:.1f} | {row['mean_s']:.6f} | "
            f"{row['ci95_s']:.6f} |"
        )
    lines.extend(
        [
            "",
            "## Fig. 2 — Cross-Layer Conflict Handling",
            "",
            "- Demand-to-Capacity Ratio is defined exactly as "
            "gamma = R_req / C_eff, where C_eff = min(C_t, C_n, C_p).",
            "- R_req is the task/application traffic requirement.",
            "- C_t is the transport-layer admissible sending/service capacity.",
            "- C_n is the bottleneck available bandwidth on the selected network path.",
            "- C_p is the available physical/access service capacity.",
            f"- Scenarios per gamma: "
            f"{exp2_config['simulation']['seeds']} consecutive seeds (0–99). "
            "For each seed, every nuisance variable and its distribution is "
            "identical across gamma; only R_req changes according to the formula.",
            "- Ground Truth is computed from the true state by exhaustive "
            "feasibility evaluation. Unresolvable scenarios remain in the raw "
            "data and count as unsatisfied; safe rejection is not converted into "
            "success.",
            f"- Highest gamma in the main figure: {highest_gamma:g}. At this point, "
            f"{highest_unresolvable}/{scenarios_per_gamma} scenarios "
            f"({100.0 * highest_unresolvable / scenarios_per_gamma:.1f}%) are "
            "intrinsically infeasible.",
            f"- {pilot_statement}",
            "",
            "| gamma | NO_CONFLICT | RESOLVABLE_CONFLICT | UNRESOLVABLE_CONFLICT |",
            "|---:|---:|---:|---:|",
        ]
    )
    for gamma in gamma_values:
        lines.append(
            f"| {gamma:g} | "
            f"{class_counts[(gamma, 'NO_CONFLICT')]} | "
            f"{class_counts[(gamma, 'RESOLVABLE_CONFLICT')]} | "
            f"{class_counts[(gamma, 'UNRESOLVABLE_CONFLICT')]} |"
        )
    lines.extend(
        [
            "",
            "The main plot intentionally covers normal operation through "
            "contention and the partially infeasible transition. A pilot-only "
            "fully infeasible region is not included because it contains no "
            "meaningful algorithm comparison; it is retained in the pilot raw "
            "artifacts and this exclusion was frozen before the formal run.",
            "",
            "## Fig. 3 — Business-Driven Reconfiguration",
            "",
            "- Fig. 3 uses the existing Exp. 3 data without rerunning or "
            "modifying any experiment result.",
            "- Panel (a) uses successful paired runs from the business-Agent "
            "count sweep and reports reconfiguration latency.",
            "- Panel (b) uses the raw changed_rules_ratio field from the removed-"
            "Agent sweep. It is labeled Rule Change Ratio (%) and is not clipped "
            "at 100%; Full Reconfiguration may legitimately exceed 100% because "
            "updates, additions, and deletions are counted by the original "
            "experiment definition.",
            "- Every plotted Exp. 3 point contains all 30 consecutive, paired "
            "seeds 0–29.",
            "",
            "## Fig. 4 — Failure Recovery with CSPF",
            "",
            "- Physical Capacity Failure is defined as "
            "C_p_after = q * R_req for q in "
            f"{exp4_config['fault_sweeps']['physical_post_failure_to_requirement_ratios']}. "
            "Every q is strictly below one.",
            f"- Proof before recovery: {physical_proof_count}/"
            f"{len(physical_unique)} unique injected physical failures have "
            "fault_was_disruptive = True, C_p_after < R_req, and a positive "
            f"violation margin. The minimum observed margin is "
            f"{minimum_physical_margin:.6f} Mbps.",
            "- CSPF implementation: (1) remove down links; (2) remove links with "
            "available/reservable bandwidth below the task-flow requirement; "
            "(3) run deterministic Dijkstra/SPF using link delay (or a fixed TE "
            "cost); (4) verify the selected path delay is no larger than the "
            "flow maximum, otherwise safely reject.",
            "- CSPF allowed information: topology, link up/down state, available "
            "or reservable bandwidth, link delay, TE/link cost, and each flow's "
            "source, destination, required bandwidth, and maximum delay.",
            "- CSPF forbidden actions: Agent replacement, application adaptation, "
            "transport-rate adaptation, physical-resource reallocation, and "
            "cross-layer proposal selection. CSPF proposals and committed "
            "changes are network-layer only.",
            f"- Same-seed paired fairness: {len(exp4_pairs)} scenario/seed pairs, "
            "each evaluated by Ours, Full Reconfiguration, and CSPF using the "
            "same scenario fingerprint, fault fingerprint, failure time, and "
            "detection time.",
            "- CSPF is excluded from Fig. 4(b), because comparing rule-change "
            "ratios for unsuccessful recovery is not meaningful.",
            "",
            "## Raw and plotting data provenance",
            "",
            "| Panel | Raw CSV source | Plotting CSV source | Seed policy |",
            "|---|---|---|---|",
            f"| Fig. 1 | {code(str(exp1_raw_path))} | "
            f"{code(str(output_dir / 'data' / 'fig1.csv'))} | all seeds 0–29 |",
            f"| Fig. 2 | {code(str(exp2_raw_path))} | "
            f"{code(str(output_dir / 'data' / 'fig2.csv'))} | all seeds 0–99 |",
            f"| Fig. 3(a) | {code(str(exp3_raw_path))} | "
            f"{code(str(output_dir / 'data' / 'fig3a.csv'))} | unchanged paired seeds 0–29 |",
            f"| Fig. 3(b) | {code(str(exp3_raw_path))} | "
            f"{code(str(output_dir / 'data' / 'fig3b.csv'))} | unchanged paired seeds 0–29 |",
            f"| Fig. 4(a) | {code(str(exp4_raw_path))} | "
            f"{code(str(output_dir / 'data' / 'fig4a.csv'))} | all paired seeds 0–29 |",
            f"| Fig. 4(b) | {code(str(exp4_raw_path))} | "
            f"{code(str(output_dir / 'data' / 'fig4b.csv'))} | all paired seeds 0–29 |",
            "",
            "### Raw source SHA-256",
            "",
            "| Artifact | SHA-256 |",
            "|---|---|",
        ]
    )
    for label, digest in hashes.items():
        lines.append(f"| {label} | {code(digest)} |")
    lines.extend(
        [
            "",
            "## Frozen publication style",
            "",
            "| Method | Color | Line style | Marker | Bar hatch |",
            "|---|---|---|---|---|",
        ]
    )
    for method in (
        "ours",
        "full_reconfiguration",
        "alc",
        "layer_wise_independent",
        "no_global_verification",
        "cspf",
    ):
        style = METHOD_STYLES[method]
        lines.append(
            f"| {style.label} | {code(style.color)} | "
            f"{code(str(style.linestyle))} | {code(style.marker)} | "
            f"{code(style.hatch or 'none')} |"
        )
    lines.extend(
        [
            "",
            "All line plots use method-specific color, line style, and marker; "
            "all grouped bars use color, edge, and hatch. Figures have a white "
            "background, serif fonts, light horizontal grids, 1.8 pt lines, "
            "5.5 pt markers, vector PDF output, and 300 dpi PNG output.",
            "",
            "## Paper captions",
            "",
            "Fig. X. Verified task-subnet formation latency under different task "
            "sizes. The reported latency is measured from task arrival to the "
            "completion of data-plane verification, including rule installation, "
            "ping-based reachability verification, and iPerf3-based throughput "
            "verification. Each point reports the mean over repeated Linux "
            "network-namespace experiments, while the shaded region denotes the "
            "95% confidence interval.",
            "",
            "Fig. X. Task satisfaction rate under increasing cross-layer resource "
            "contention. The demand-to-capacity ratio is defined as the task "
            "traffic requirement divided by the effective end-to-end support "
            "capacity. ALC coordinates only adjacent layers, whereas Layer-wise "
            "Independent makes decisions independently at each layer. Ours "
            "jointly evaluates application-, transport-, network-, and physical-"
            "layer actions. At high demand-to-capacity ratios, some scenarios may "
            "become intrinsically infeasible; Ours safely rejects such cases "
            "rather than installing globally invalid configurations.",
            "",
            "Fig. X. Runtime reconfiguration performance under business changes. "
            "(a) Reconfiguration latency versus the number of business agents. "
            "(b) Rule change ratio versus the fraction of removed agents. Ours "
            "performs dependency-aware incremental updates, whereas Full "
            "Reconfiguration reconstructs the complete task subnet.",
            "",
            "Fig. X. Failure-recovery performance under heterogeneous runtime "
            "failures. (a) Recovery success rates of Ours, Full Reconfiguration, "
            "and CSPF for agent, link, and physical-capacity failures. CSPF "
            "performs constrained network-layer rerouting and therefore cannot "
            "recover failures that require agent replacement or cross-layer "
            "resource adaptation. (b) Rule change ratio of Ours and Full "
            "Reconfiguration. Ours limits reconfiguration to the affected "
            "dependency scope, whereas Full Reconfiguration updates the entire "
            "task subnet.",
            "",
        ]
    )
    (output_dir / "experiment_report.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
        newline="\n",
    )


def _continuous_interval(
    values: Sequence[float],
) -> tuple[float, float, float, float]:
    if not values:
        raise ValueError("cannot calculate a confidence interval without samples")
    mean = statistics.fmean(values)
    standard_deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    half = 1.96 * standard_deviation / math.sqrt(len(values))
    return mean, mean - half, mean + half, half


def _wilson_interval(
    successes: int,
    samples: int,
    z: float = 1.96,
) -> tuple[float, float, float, float]:
    if samples <= 0:
        raise ValueError("Wilson interval requires at least one sample")
    proportion = successes / samples
    denominator = 1.0 + z * z / samples
    center = (proportion + z * z / (2.0 * samples)) / denominator
    half = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / samples
            + z * z / (4.0 * samples * samples)
        )
        / denominator
    )
    lower = max(0.0, center - half)
    upper = min(1.0, center + half)
    display_half = max(proportion - lower, upper - proportion)
    return proportion, lower, upper, display_half


def _percentage_upper_bound(maximum: float) -> float:
    if maximum <= 100.0 + 1e-9:
        return 100.0
    if maximum <= 120.0:
        return 120.0
    if maximum <= 150.0:
        return 150.0
    return 10.0 * math.ceil(maximum / 10.0)


def _failure_family(fault_type: str) -> str:
    if fault_type == "AGENT_FAILURE":
        return "Agent Failure"
    if fault_type in {"LINK_FAILURE", "LINK_DEGRADATION"}:
        return "Link Failure"
    if fault_type == "PHYSICAL_CAPACITY_DROP":
        return "Physical Capacity Failure"
    raise ValueError(f"unknown failure type: {fault_type}")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: Iterable[dict[str, object]]) -> None:
    materialized = list(rows)
    if not materialized:
        raise ValueError(f"refusing to write empty plotting CSV: {path}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(materialized[0]),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(materialized)


def _save(fig, base: Path) -> None:
    fig.savefig(
        base.with_suffix(".pdf"),
        format="pdf",
        bbox_inches="tight",
    )
    fig.savefig(
        base.with_suffix(".png"),
        format="png",
        dpi=300,
        bbox_inches="tight",
    )
    plt.close(fig)


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_yaml(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _md_code(value: object) -> str:
    marker = chr(96)
    return marker + str(value) + marker


def _fence(language: str = "") -> str:
    return chr(96) * 3 + language


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate the final WCNC four-figure, six-panel package"
    )
    parser.add_argument("--exp1-dir", default="results/exp1_wcnc_final")
    parser.add_argument("--exp2-dir", default="results/exp2_demand_capacity")
    parser.add_argument("--exp3-dir", default="results/exp3")
    parser.add_argument("--exp4-dir", default="results/exp4_cspf_final")
    parser.add_argument("--output-dir", default="results/wcnc_main_final")
    return parser


def main() -> None:
    args = _parser().parse_args()
    generate(
        exp1_dir=Path(args.exp1_dir),
        exp2_dir=Path(args.exp2_dir),
        exp3_dir=Path(args.exp3_dir),
        exp4_dir=Path(args.exp4_dir),
        output_dir=Path(args.output_dir),
    )
    print(f"Generated final WCNC figures and report in {Path(args.output_dir)}")


if __name__ == "__main__":
    main()
