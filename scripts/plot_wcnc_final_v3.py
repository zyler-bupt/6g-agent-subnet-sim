from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt

plt.rcParams["svg.hashsalt"] = "wcnc_final_v3"
plt.rcParams.update({"font.size": 7.5, "axes.labelsize": 8, "legend.fontsize": 6.8,
                     "xtick.labelsize": 7, "ytick.labelsize": 7})


LABELS = {
    "proposed": "Ours", "cspf": "CSPF Recovery", "global_sfc_embedding": "Global SFC Embedding",
    "sanet_dw": "SANet-DW*", "weighted_sum": "Weighted-Sum", "independent": "Independent",
    "netren": "NetRen*", "local_only": "Local-Only", "full_rebuild": "Full Rebuild",
    "sfc_restoration": "SFC Restoration", "te_reopt": "TE Re-optimization",
}
COLORS = {"proposed": "#0072B2", "sanet_dw": "#D55E00", "netren": "#D55E00", "cspf": "#009E73",
          "global_sfc_embedding": "#CC79A7", "weighted_sum": "#E69F00", "independent": "#777777",
          "local_only": "#009E73", "full_rebuild": "#555555", "sfc_restoration": "#CC79A7", "te_reopt": "#E69F00"}
MARKERS = ("o", "s", "^", "D")
TRANSACTIONAL_METHODS = ("proposed", "cspf", "global_sfc_embedding")
TRANSACTIONAL_SCENARIOS = (
    "stale_version", "prepare_ack_timeout", "command_rejection",
)
TRANSACTIONAL_METRICS = (
    "method_owned_formation_latency_ms", "time_to_correct_formation_ms",
    "rollback_scope_objects", "wasted_rule_commands",
)
TRANSACTIONAL_MARKERS = {
    "proposed": "o", "cspf": "s", "global_sfc_embedding": "^",
}
TRANSACTIONAL_SCENARIO_LABELS = {
    "stale_version": "Stale version",
    "prepare_ack_timeout": "Prepare-ACK timeout",
    "command_rejection": "Command rejection",
}


def load(path: Path) -> list[dict[str, str]]:
    if "demo" in str(path).lower() or "synthetic" in str(path).lower():
        raise ValueError("formal plotter refuses demo/synthetic inputs")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if any(row.get("protocol_id") != "wcnc_final_v3" for row in rows):
        raise ValueError("aggregated input is not canonical wcnc_final_v3")
    return rows


def display_label(experiment: str, method: str) -> str:
    if method == "cspf" and experiment == "exp1":
        return "CSPF-based Formation"
    return LABELS.get(method, method)


def plot_metric(rows, metric: str, output: Path, *, series: str = "") -> None:
    selected = [row for row in rows if row["metric"] == metric and (not series or row["series"] == series)]
    if not selected:
        return
    fig, ax = plt.subplots(figsize=(3.45, 2.55), constrained_layout=True)
    methods = sorted({row["method_id"] for row in selected}, key=lambda value: list(LABELS).index(value) if value in LABELS else 99)
    for index, method in enumerate(methods):
        data = sorted((row for row in selected if row["method_id"] == method), key=lambda row: float(row["x_value"]))
        x = [float(row["x_value"]) for row in data]; y = [float(row["estimate"]) for row in data]
        low = [max(0.0, yv - float(row["ci_low"])) for yv, row in zip(y, data)]
        high = [max(0.0, float(row["ci_high"]) - yv) for yv, row in zip(y, data)]
        ax.errorbar(x, y, yerr=[low, high], label=display_label(selected[0]["experiment"], method), color=COLORS.get(method),
                    marker=MARKERS[index % len(MARKERS)], linewidth=1.5, markersize=4, capsize=2)
    axis_labels = {
        "gamma": r"Load factor $\gamma$", "num_agents": "Number of Agents",
        "affected_dependency_scope_percent": "Affected scope (%)",
        "affected_flow_ratio": "Affected-flow ratio", "dependency_closure_ratio": "Dependency-closure ratio",
        "post_fault_capacity_ratio": "Post-fault capacity / demand",
    }
    metric_labels = {
        "success_rate": "Success rate", "conditional_verified_latency_ms": "Conditional verified latency (ms)",
        "route_install_latency_ms": "Route-installation latency (ms)",
        "feasible_qos_satisfaction_rate": "Feasible-scenario QoS rate",
        "pre_verification_correct_decision_rate": "Pre-verification correct decision rate",
        "modification_scope_ratio": "Modification scope", "unaffected_flow_interruption": "Unaffected-flow interruption",
    }
    ax.set_xlabel(axis_labels.get(selected[0]["x_name"], selected[0]["x_name"].replace("_", " ").title()))
    ax.set_ylabel(metric_labels.get(metric, metric.replace("_", " ").title()))
    ax.grid(True, linestyle=":", linewidth=0.5, alpha=0.6); ax.legend(frameon=False, fontsize=7)
    output.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("pdf", "png", "svg"):
        metadata = ({"Creator": "wcnc_final_v3", "CreationDate": None, "ModDate": None}
                    if suffix == "pdf" else {"Creator": "wcnc_final_v3"})
        fig.savefig(output.with_suffix(f".{suffix}"), dpi=300, metadata=metadata)
    plt.close(fig)


def _finite(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _save_figure(fig, output: Path) -> list[Path]:
    output.parent.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for suffix in ("pdf", "png", "svg"):
        target = output.with_suffix(f".{suffix}")
        if suffix == "pdf":
            metadata = {"Creator": "wcnc_final_v3", "CreationDate": None, "ModDate": None}
        elif suffix == "svg":
            metadata = {"Creator": "wcnc_final_v3", "Date": None}
        else:
            metadata = {"Software": "wcnc_final_v3"}
        fig.savefig(target, dpi=300, metadata=metadata)
        written.append(target)
    return written


def plot_transactional_metric(rows: list[dict[str, str]], metric: str, output: Path) -> list[Path]:
    """Plot observed aggregate points only; missing cells stay visibly absent."""
    selected = [
        row for row in rows
        if row.get("metric") == metric
        and row.get("series") in TRANSACTIONAL_SCENARIOS
        and row.get("method_id") in TRANSACTIONAL_METHODS
        and row.get("x_name") == "num_agents"
    ]
    x_values = sorted({value for row in selected if (value := _finite(row.get("x_value"))) is not None})
    if not x_values:
        x_values = [4.0, 8.0, 12.0, 16.0, 20.0]
    plt.rcParams.update({
        "font.family": "serif", "font.serif": ["STIX Two Text", "DejaVu Serif"],
        "font.size": 7.5, "axes.labelsize": 8, "legend.fontsize": 6.6,
        "xtick.labelsize": 7, "ytick.labelsize": 7,
    })
    fig, axes = plt.subplots(1, 3, figsize=(7.15, 2.35), sharey=True, constrained_layout=True)
    legend_handles = []
    for axis, scenario in zip(axes, TRANSACTIONAL_SCENARIOS):
        scenario_rows = [row for row in selected if row.get("series") == scenario]
        axis.set_title(TRANSACTIONAL_SCENARIO_LABELS[scenario], fontsize=8, fontweight="normal")
        any_visible = False
        for method in TRANSACTIONAL_METHODS:
            indexed = {
                _finite(row.get("x_value")): row
                for row in scenario_rows if row.get("method_id") == method
                and _finite(row.get("x_value")) is not None
                and (_finite(row.get("denominator")) or 0.0) > 0.0
            }
            estimate = [_finite(indexed.get(x, {}).get("estimate")) for x in x_values]
            ci_low = [_finite(indexed.get(x, {}).get("ci_low")) for x in x_values]
            ci_high = [_finite(indexed.get(x, {}).get("ci_high")) for x in x_values]
            y = [value if value is not None else math.nan for value in estimate]
            low = [value if value is not None else math.nan for value in ci_low]
            high = [value if value is not None else math.nan for value in ci_high]
            if not any(math.isfinite(value) for value in y):
                continue
            any_visible = True
            color = COLORS[method]
            axis.fill_between(x_values, low, high, color=color, alpha=0.15, linewidth=0)
            line, = axis.plot(
                x_values, y, color=color, marker=TRANSACTIONAL_MARKERS[method],
                linewidth=1.8, markersize=4.4,
                label=display_label("exp1", method),
            )
            if not legend_handles:
                legend_handles.append(line)
            elif method not in {handle.get_label() for handle in legend_handles}:
                legend_handles.append(line)
        if not any_visible:
            axis.text(0.5, 0.5, "N/A", ha="center", va="center", transform=axis.transAxes)
        axis.set_xticks(x_values)
        axis.set_xlabel("Number of agents")
        axis.tick_params(direction="out", length=3, width=0.8)
        for side, spine in axis.spines.items():
            spine.set_visible(side in {"left", "bottom"})
            spine.set_linewidth(0.9)
        axis.grid(False)
    metric_labels = {
        "method_owned_formation_latency_ms": "Method-owned formation latency (ms)",
        "time_to_correct_formation_ms": "Time to correct formation (ms)",
        "rollback_scope_objects": "Rollback scope (objects)",
        "wasted_rule_commands": "Wasted rule commands",
    }
    axes[0].set_ylabel(metric_labels[metric])
    if legend_handles:
        fig.legend(legend_handles, [handle.get_label() for handle in legend_handles],
                   loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.06))
    written = _save_figure(fig, output)
    plt.close(fig)
    return written


def _write_transactional_figure_manifest(root: Path, metrics_path: Path, figures: list[Path]) -> None:
    manifest_path = root / "figures" / "figure_manifest.json"
    manifest = {
        "protocol_id": "wcnc_final_v3",
        "experiment": "exp1_transactional",
        "input_metrics_path": str(metrics_path.relative_to(root)),
        "input_metrics_sha256": hashlib.sha256(metrics_path.read_bytes()).hexdigest(),
        "plotting_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "figure_hashes": {
            str(path.relative_to(root / "figures")): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(figures)
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--root", default="results/paper/wcnc_final_v3"); parser.add_argument("--experiment", choices=("all", "exp1", "exp1_transactional", "exp2", "exp3", "exp4"), default="all")
    args = parser.parse_args(); root = Path(args.root)
    requests = {
        "exp1": (("conditional_verified_latency_ms", ""), ("route_install_latency_ms", "")),
        "exp2": (("feasible_qos_satisfaction_rate", ""), ("pre_verification_correct_decision_rate", "")),
        "exp3": (("success_rate", ""), ("conditional_verified_latency_ms", ""), ("modification_scope_ratio", "")),
        "exp4": tuple((metric, series) for series in ("link_failure", "agent_failure", "capacity_degradation") for metric in ("success_rate", "conditional_verified_latency_ms", "modification_scope_ratio")),
    }
    selected_experiments = (
        requests if args.experiment == "all"
        else ({args.experiment: requests[args.experiment]} if args.experiment in requests else {})
    )
    for exp, figures in selected_experiments.items():
        rows = load(root / "aggregated" / exp / "metrics.csv")
        for metric, series in figures:
            name = f"{exp}_{series + '_' if series else ''}{metric}"
            plot_metric(rows, metric, root / "figures" / name, series=series)
    if args.experiment in {"all", "exp1_transactional"}:
        metrics_path = root / "aggregated" / "exp1_transactional" / "metrics.csv"
        if metrics_path.exists():
            rows = load(metrics_path)
            written = []
            for metric in TRANSACTIONAL_METRICS:
                written.extend(plot_transactional_metric(
                    rows, metric, root / "figures" / f"exp1_transactional_{metric}",
                ))
            _write_transactional_figure_manifest(root, metrics_path, written)


if __name__ == "__main__": main()
