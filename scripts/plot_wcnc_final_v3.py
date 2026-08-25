from __future__ import annotations

import argparse
import csv
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


def load(path: Path) -> list[dict[str, str]]:
    if "demo" in str(path).lower() or "synthetic" in str(path).lower():
        raise ValueError("formal plotter refuses demo/synthetic inputs")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if any(row.get("protocol_id") != "wcnc_final_v3" for row in rows):
        raise ValueError("aggregated input is not canonical wcnc_final_v3")
    return rows


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
        ax.errorbar(x, y, yerr=[low, high], label=LABELS.get(method, method), color=COLORS.get(method),
                    marker=MARKERS[index % len(MARKERS)], linewidth=1.5, markersize=4, capsize=2)
    axis_labels = {
        "gamma": r"Load factor $\gamma$", "num_agents": "Number of Agents",
        "affected_dependency_scope_percent": "Affected scope (%)",
        "affected_flow_ratio": "Affected-flow ratio", "dependency_closure_ratio": "Dependency-closure ratio",
        "post_fault_capacity_ratio": "Post-fault capacity / demand",
    }
    metric_labels = {
        "success_rate": "Success rate", "conditional_verified_latency_ms": "Conditional verified latency (ms)",
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


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--root", default="results/paper/wcnc_final_v3"); parser.add_argument("--experiment", choices=("all", "exp1", "exp2", "exp3", "exp4"), default="all")
    args = parser.parse_args(); root = Path(args.root)
    requests = {
        "exp1": (("success_rate", ""), ("conditional_verified_latency_ms", "")),
        "exp2": (("feasible_qos_satisfaction_rate", ""), ("pre_verification_correct_decision_rate", "")),
        "exp3": (("success_rate", ""), ("conditional_verified_latency_ms", ""), ("modification_scope_ratio", "")),
        "exp4": tuple((metric, series) for series in ("link_failure", "agent_failure", "capacity_degradation") for metric in ("success_rate", "conditional_verified_latency_ms", "modification_scope_ratio")),
    }
    selected_experiments = requests if args.experiment == "all" else {args.experiment: requests[args.experiment]}
    for exp, figures in selected_experiments.items():
        rows = load(root / "aggregated" / exp / "metrics.csv")
        for metric, series in figures:
            name = f"{exp}_{series + '_' if series else ''}{metric}"
            plot_metric(rows, metric, root / "figures" / name, series=series)


if __name__ == "__main__": main()
