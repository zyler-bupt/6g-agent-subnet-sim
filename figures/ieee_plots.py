"""IEEE communication-paper figure generator for the WCNC experiments.

Produces the eight sub-figures required by the experiment guide:

    Fig.1 Formation        (a) formation latency   (b) success rate
    Fig.2 Coordination     (a) QoS satisfaction    (b) feasible solution rate
    Fig.3 Elasticity       (a) reconfig latency    (b) scope-success tradeoff
    Fig.4 Recovery         (a) recovery latency    (b) modification scope

Style contract (from the guide)
-------------------------------
* Font: Times New Roman (falls back to Times / DejaVu Serif if absent).
* Axis 10 pt, tick 9 pt, legend 8-9 pt.
* White background, light grid, vector PDF output.
* Consistent colours: Proposed green #2ca25f solid diamond;
  baselines red #e74c3c / orange #f39c12 / blue #3498db; ablation gray #7f7f7f.
  The SAME method_id always uses the SAME colour/marker/linestyle.

Input contract
--------------
`build_figures` reads ONE aggregated CSV with columns:
    experiment, subplot, method, x, y_mean, y_lo, y_hi
where (experiment, subplot) identifies one of the eight sub-figures and
y_lo / y_hi are the 95% confidence (or bootstrap) bounds. Categorical x
(e.g. failure type) is given as a small integer 0,1,2,... and a label map
in CATEGORICAL_X below.

`--demo` synthesizes plausible data following the guide's expected trends so
the visual style can be validated before the real pipeline produces results.
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")  # headless / CI safe
import matplotlib.pyplot as plt
from matplotlib import font_manager  # noqa: F401  (ensures font cache build)

# ---------------------------------------------------------------------------
# IEEE style
# ---------------------------------------------------------------------------

GUIDE_FONT_SIZES = {
    "axes.titlesize": 10,
    "axes.labelsize": 10,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 8,
    "font.size": 9,
}


def apply_ieee_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
            "mathtext.fontset": "stix",
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": "#333333",
            "axes.grid": True,
            "grid.color": "#dddddd",
            "grid.linewidth": 0.6,
            "grid.alpha": 0.9,
            "axes.axisbelow": True,
            "lines.linewidth": 1.6,
            "lines.markersize": 6,
            "legend.frameon": False,
            "legend.loc": "best",
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "pdf.fonttype": 42,  # embed TrueType, editable in Illustrator
            "ps.fonttype": 42,
        }
    )
    plt.rcParams.update(GUIDE_FONT_SIZES)


# ---------------------------------------------------------------------------
# Consistent method styling (same method_id -> same colour everywhere)
# ---------------------------------------------------------------------------

PROPOSED_GREEN = "#2ca25f"
B1_RED = "#e74c3c"
B2_ORANGE = "#f39c12"
B3_BLUE = "#3498db"
ABLATION_GRAY = "#7f7f7f"
EXTRA_PURPLE = "#8e44ad"
EXTRA_BROWN = "#795548"
EXTRA_PINK = "#e84393"

MARKERS = {
    "diamond": "D",
    "square": "s",
    "triangle": "^",
    "circle": "o",
    "x": "x",
    "plus": "P",
    "star": "*",
}

LINESTYLES = {
    "solid": "-",
    "dashed": "--",
    "dotted": ":",
    "dashdot": "-.",
}

METHOD_STYLE: dict[str, dict] = {
    # Proposed is ALWAYS green / solid / diamond.
    "proposed": {"color": PROPOSED_GREEN, "marker": "diamond", "ls": "solid", "label": "Proposed"},
    "proposed_without_batch": {
        "color": PROPOSED_GREEN, "marker": "circle", "ls": "dotted", "label": "Proposed w/o Batch",
    },
    # Baseline pool (distinct per experiment via these assignments).
    "cspf": {"color": B1_RED, "marker": "square", "ls": "dashed", "label": "CSPF"},
    "ilp_sfc": {"color": B2_ORANGE, "marker": "triangle", "ls": "dotted", "label": "ILP-SFC"},
    "sfc_reoptimization": {"color": B3_BLUE, "marker": "circle", "ls": "dotted", "label": "SFC-Reopt"},
    "sanet_dw": {"color": B1_RED, "marker": "square", "ls": "dashed", "label": "SANet-DW*"},
    "adjacent_layer": {"color": B2_ORANGE, "marker": "triangle", "ls": "dotted", "label": "Adjacent-Layer"},
    "independent": {"color": B3_BLUE, "marker": "circle", "ls": "dotted", "label": "Independent"},
    "weighted_sum": {"color": EXTRA_PURPLE, "marker": "star", "ls": "dotted", "label": "Weighted-Sum"},
    "netren": {"color": B2_ORANGE, "marker": "triangle", "ls": "dotted", "label": "NetRen*"},
    "sfc_reconfiguration": {"color": B3_BLUE, "marker": "circle", "ls": "dotted", "label": "SFC-Reconfig*"},
    "local_only": {"color": ABLATION_GRAY, "marker": "x", "ls": "dotted", "label": "Local-Only"},
    "full_rebuild": {"color": ABLATION_GRAY, "marker": "plus", "ls": "dashed", "label": "Full-Rebuild"},
    "netkeeper": {"color": B1_RED, "marker": "square", "ls": "dashed", "label": "NetKeeper*"},
    "frr": {"color": B2_ORANGE, "marker": "triangle", "ls": "dotted", "label": "FRR"},
    "te_reopt": {"color": B3_BLUE, "marker": "circle", "ls": "dotted", "label": "TE-Reopt"},
    "sfc_restoration": {"color": EXTRA_PURPLE, "marker": "star", "ls": "dotted", "label": "SFC-Restore"},
}


def style_for(method_id: str) -> dict:
    if method_id not in METHOD_STYLE:
        raise KeyError(f"no IEEE style registered for method_id={method_id!r}")
    spec = METHOD_STYLE[method_id]
    return {
        "color": spec["color"],
        "marker": MARKERS[spec["marker"]],
        "linestyle": LINESTYLES[spec["ls"]],
        "label": spec["label"],
    }


# Categorical x-axis labels for the recovery experiment.
CATEGORICAL_X = {
    "exp4": {
        0: "Link",
        1: "Agent",
        2: "Capacity",
    }
}

# (experiment, subplot) -> (title, xlabel, ylabel, y_percent)
SUBFIGURE_META = {
    ("exp1", "a"): ("Formation Latency", "Task Size (agents)", "E2E Formation (ms)", False),
    ("exp1", "b"): ("Formation Success Rate", "Task Size (agents)", "Success Rate (%)", True),
    ("exp2", "a"): ("QoS Satisfaction", "Conflict Density (%)", "QoS Satisfaction (%)", True),
    ("exp2", "b"): ("Feasible Solution Rate", "Conflict Density (%)", "Feasible Rate (%)", True),
    ("exp3", "a"): ("Reconfiguration Latency", "Changed Agents", "Latency (ms)", False),
    ("exp3", "b"): ("Scope vs Success Tradeoff", "Modification Scope (%)", "Success Rate (%)", True),
    ("exp4", "a"): ("Recovery Latency", "Failure Type", "Recovery (ms)", False),
    ("exp4", "b"): ("Modification Scope", "Failure Type", "Changed Scope (%)", True),
}

FIGURE_LAYOUT = {
    "fig1": [("exp1", "a"), ("exp1", "b")],
    "fig2": [("exp2", "a"), ("exp2", "b")],
    "fig3": [("exp3", "a"), ("exp3", "b")],
    "fig4": [("exp4", "a"), ("exp4", "b")],
}


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _ordered_methods(methods: Iterable[str]) -> list[str]:
    """Plot Proposed first, then proposed-without-batch, then baselines sorted."""
    unique = set(methods)
    order = [m for m in ("proposed", "proposed_without_batch") if m in unique]
    order.extend(sorted(unique - set(order)))
    return order


def _plot_subfigure(ax, rows: list[dict], experiment: str, subplot: str) -> None:
    title, xlabel, ylabel, is_pct = SUBFIGURE_META[(experiment, subplot)]
    methods = _ordered_methods({r["method"] for r in rows})
    # group by method
    by_method: dict[str, list[dict]] = {m: [] for m in methods}
    for r in rows:
        by_method[r["method"]].append(r)
    for m in methods:
        series = sorted(by_method[m], key=lambda r: r["x"])
        xs = [r["x"] for r in series]
        ys = [r["y_mean"] for r in series]
        lo = [r["y_mean"] - r["y_lo"] for r in series]
        hi = [r["y_hi"] - r["y_mean"] for r in series]
        st = style_for(m)
        ax.plot(
            xs, ys,
            color=st["color"], marker=st["marker"], linestyle=st["linestyle"],
            label=st["label"], linewidth=1.6, markersize=6,
        )
        ax.fill_between(xs, lo, hi, color=st["color"], alpha=0.12, linewidth=0)
    # x ticks: categorical for recovery
    if experiment in CATEGORICAL_X:
        ticks = sorted(CATEGORICAL_X[experiment].keys())
        ax.set_xticks(ticks)
        ax.set_xticklabels([CATEGORICAL_X[experiment][t] for t in ticks])
    ax.set_title(title, fontsize=10)
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    if is_pct:
        ax.set_ylim(0, 105)
    ax.legend(fontsize=8, frameon=False, loc="best")


def build_figures(aggregated_csv: Path, out_dir: Path) -> list[Path]:
    """Read the aggregated CSV and write Fig.1-4 as PDF + PNG."""
    apply_ieee_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    with aggregated_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                {
                    "experiment": row["experiment"],
                    "subplot": row["subplot"],
                    "method": row["method"],
                    "x": float(row["x"]),
                    "y_mean": float(row["y_mean"]),
                    "y_lo": float(row["y_lo"]),
                    "y_hi": float(row["y_hi"]),
                }
            )
    written: list[Path] = []
    for fig_name, subfigs in FIGURE_LAYOUT.items():
        fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.6))
        for ax, (exp, sub) in zip(axes, subfigs):
            sub_rows = [
                r for r in rows if r["experiment"] == exp and r["subplot"] == sub
            ]
            if not sub_rows:
                ax.set_title(f"{exp}/{sub} (no data)", fontsize=9)
                continue
            _plot_subfigure(ax, sub_rows, exp, sub)
        fig.tight_layout()
        pdf = out_dir / f"{fig_name}.pdf"
        png = out_dir / f"{fig_name}.png"
        fig.savefig(pdf)
        fig.savefig(png, dpi=300)
        plt.close(fig)
        written.extend([pdf, png])
    return written


# ---------------------------------------------------------------------------
# Demo data (follows the guide's expected trends)
# ---------------------------------------------------------------------------

def _demo_rows() -> list[dict]:
    rows: list[dict] = []
    rng_state = 1234

    def jitter(spread: float) -> float:
        nonlocal rng_state
        rng_state = (1103515245 * rng_state + 12345) & 0x7FFFFFFF
        return (rng_state / 0x7FFFFFFF - 0.5) * 2 * spread

    # Exp1: formation latency rises with task size; proposed < baselines.
    sizes = [8, 12, 16, 20, 24, 28, 32]
    base = {"proposed": 850, "cspf": 1700, "ilp_sfc": 1750, "sfc_reoptimization": 1730}
    slope = {"proposed": 55, "cspf": 165, "ilp_sfc": 168, "sfc_reoptimization": 166}
    for m, b in base.items():
        for x in sizes:
            y = b + slope[m] * (x - 8)
            rows.append({"experiment": "exp1", "subplot": "a", "method": m, "x": x,
                         "y_mean": y, "y_lo": y - 60, "y_hi": y + 60})
            sr = 100 - 0.15 * (x - 8) if m == "proposed" else max(80, 99 - 1.2 * (x - 8))
            rows.append({"experiment": "exp1", "subplot": "b", "method": m, "x": x,
                         "y_mean": sr, "y_lo": sr - 3, "y_hi": sr + 3})

    # Exp2: conflict density 0..60; proposed stays high, independent drops most.
    densities = [0, 10, 20, 30, 40, 50, 60]
    qos = {
        "proposed": lambda d: 99 - 0.05 * d,
        "sanet_dw": lambda d: 98 - 0.25 * d,
        "weighted_sum": lambda d: 97 - 0.7 * d,
        "independent": lambda d: 96 - 1.5 * d,
        "adjacent_layer": lambda d: 97 - 1.1 * d,
    }
    for m, fn in qos.items():
        for d in densities:
            y = max(20, fn(d) + jitter(1.5))
            rows.append({"experiment": "exp2", "subplot": "a", "method": m, "x": d,
                         "y_mean": y, "y_lo": y - 3, "y_hi": y + 3})
            fr = max(15, y - 4 + jitter(2))
            rows.append({"experiment": "exp2", "subplot": "b", "method": m, "x": d,
                         "y_mean": fr, "y_lo": fr - 3, "y_hi": fr + 3})

    # Exp3: changed agents 1..8; proposed small scope + high success.
    ncs = [1, 2, 4, 8]
    scope_of = {
        "proposed": lambda x: 8 + 3 * x,
        "full_rebuild": lambda x: 95 + 2 * x,
        "local_only": lambda x: 4 + x,
        "netren": lambda x: 40 + 8 * x,
        "sfc_reconfiguration": lambda x: 40 + 8 * x,
    }
    succ_of = {
        "proposed": lambda x: 99 - 0.4 * x,
        "full_rebuild": lambda x: 99,
        "local_only": lambda x: 70 + 3 * x,
        "netren": lambda x: 90 + 1 * x,
        "sfc_reconfiguration": lambda x: 90 + 1 * x,
    }
    for m in scope_of:
        for x in ncs:
            lat = {"proposed": 300 + 40 * x, "full_rebuild": 1200 + 200 * x}.get(
                m, 700 + 90 * x
            )
            rows.append({"experiment": "exp3", "subplot": "a", "method": m, "x": x,
                         "y_mean": lat, "y_lo": lat - 80, "y_hi": lat + 80})
            # exp3(b): scope-success tradeoff (x = scope %, y = success %)
            scope = scope_of[m](x)
            succ = succ_of[m](x)
            rows.append({"experiment": "exp3", "subplot": "b", "method": m, "x": scope,
                         "y_mean": succ, "y_lo": succ - 3, "y_hi": succ + 3})

    # Exp4: failure types 0=Link,1=Agent,2=Capacity.
    for m in ("proposed", "netkeeper", "cspf", "full_rebuild", "frr", "te_reopt", "sfc_restoration"):
        for xi, ft in enumerate(("Link", "Agent", "Capacity")):
            if m == "proposed":
                lat = {"Link": 900, "Agent": 700, "Capacity": 800}[ft]
            elif m == "cspf" and ft == "Link":
                lat = 600
            elif m == "frr" and ft == "Link":
                lat = 550
            elif m == "netkeeper" and ft == "Agent":
                lat = 750
            elif ft == "Agent":
                lat = 1500 if m in ("cspf", "frr") else 1000
            elif ft == "Capacity":
                lat = 700 if m == "te_reopt" else 1300
            else:
                lat = 1000
            rows.append({"experiment": "exp4", "subplot": "a", "method": m, "x": xi,
                         "y_mean": lat, "y_lo": lat - 90, "y_hi": lat + 90})
            scope = {"proposed": 20, "full_rebuild": 95, "cspf": 60,
                     "netkeeper": 25, "frr": 55, "te_reopt": 35, "sfc_restoration": 40}[m]
            rows.append({"experiment": "exp4", "subplot": "b", "method": m, "x": xi,
                         "y_mean": scope, "y_lo": scope - 5, "y_hi": scope + 5})
    return rows


def demo(out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    agg = out_dir / "aggregated_demo.csv"
    with agg.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["experiment", "subplot", "method", "x", "y_mean", "y_lo", "y_hi"]
        )
        writer.writeheader()
        for r in _demo_rows():
            writer.writerow({k: r[k] for k in writer.fieldnames})
    return build_figures(agg, out_dir)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate IEEE-style WCNC experiment figures")
    parser.add_argument("--aggregated", type=Path, help="aggregated CSV path")
    parser.add_argument("--out", type=Path, default=Path("figures/out"))
    parser.add_argument("--demo", action="store_true", help="synthesize demo data and render")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.demo:
        paths = demo(args.out)
        print(f"Demo figures written: {[str(p) for p in paths]}")
    elif args.aggregated:
        paths = build_figures(Path(args.aggregated), Path(args.out))
        print(f"Figures written: {[str(p) for p in paths]}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
