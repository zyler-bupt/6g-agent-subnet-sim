"""IEEE communication-paper figure generator for the WCNC 2027 experiments.

Produces the eight sub-figures required by experiment revision v2:

    Fig.1 Formation        (a) formation latency   (b) success rate (bar)
    Fig.2 Coordination     (a) QoS satisfaction    (b) safe rejection rate
    Fig.3 Elasticity       (a) reconfig latency    (b) scope-success (scatter)
    Fig.4 Recovery         (a) recovery latency    (b) modification scope
                                                  (both grouped by failure type)

Style contract (from the guide)
-------------------------------
* Font: Times New Roman (falls back to Times / DejaVu Serif if absent).
* Axis 10 pt, tick 9 pt, legend 8-9 pt.
* White background, light grid, vector PDF output.
* Proposed green #2ca25f solid diamond; baselines red #e74c3c (B1) /
  orange #f39c12 (B2) / blue #3498db (B3); ablation gray #7f7f7f.
  The SAME method_id always uses the SAME colour/marker/linestyle.

Input contract
--------------
`build_figures` reads ONE aggregated CSV with columns:
    experiment, subplot, method, x, y_mean, y_lo, y_hi
where (experiment, subplot) identifies one of the eight sub-figures and
y_lo / y_hi are the 95% confidence (or bootstrap) bounds. Categorical x
(e.g. failure type, or method for a bar chart) is given as a small integer
0,1,2,... with a label map in CATEGORICAL_X below.

`--demo` synthesizes plausible data following the guide's expected trends so
the visual style and layout can be validated before the real pipeline runs.
"""

from __future__ import annotations

import argparse
import csv
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

# v2 frozen method sets. Baseline role -> colour per experiment:
#   Exp1 B1=CSPF(red)  B2=SFC Re-opt(orange)
#   Exp2 B1=Independent(red) B2=Weighted-Sum(orange) B3=SANet(blue)
#   Exp3 B1=Local-Only(red) B2=NetRen(orange)       B3=Full Rebuild(blue)
#   Exp4 link: CSPF(red); capacity: TE-Reopt(blue) + Full Rebuild(blue)
METHOD_STYLE: dict[str, dict] = {
    # Proposed is ALWAYS green / solid / diamond.
    "proposed": {"color": PROPOSED_GREEN, "marker": "diamond", "ls": "solid", "label": "Proposed"},
    # Exp1
    "cspf": {"color": B1_RED, "marker": "square", "ls": "dashed", "label": "CSPF"},
    "sfc_reoptimization": {"color": B2_ORANGE, "marker": "triangle", "ls": "dashed", "label": "SFC Re-opt"},
    # Exp2
    "independent": {"color": B1_RED, "marker": "square", "ls": "dashed", "label": "Independent"},
    "weighted_sum": {"color": B2_ORANGE, "marker": "star", "ls": "dashed", "label": "Weighted-Sum"},
    "sanet_dw": {"color": B3_BLUE, "marker": "circle", "ls": "dashed", "label": "SANet*"},
    # Exp3
    "local_only": {"color": B1_RED, "marker": "x", "ls": "dashed", "label": "Local-Only"},
    "netren": {"color": B2_ORANGE, "marker": "triangle", "ls": "dashed", "label": "NetRen*"},
    "full_rebuild": {"color": B3_BLUE, "marker": "plus", "ls": "dashed", "label": "Full Rebuild"},
    # Exp4 recovery (failure-specific baselines; no mixed comparison)
    "network_only": {"color": ABLATION_GRAY, "marker": "circle", "ls": "dotted", "label": "Network-Only"},
    "frr": {"color": B2_ORANGE, "marker": "triangle", "ls": "dotted", "label": "FRR"},
    "te_reopt": {"color": B3_BLUE, "marker": "circle", "ls": "dashed", "label": "TE-Reopt"},
    "sfc_restoration": {"color": EXTRA_PURPLE, "marker": "star", "ls": "dashed", "label": "SFC-Restore"},
    # Appendix / internal ablation only (kept for reference, not in main figures)
    "ilp_sfc": {"color": B2_ORANGE, "marker": "triangle", "ls": "dotted", "label": "ILP-SFC*"},
    "adjacent_layer": {"color": ABLATION_GRAY, "marker": "triangle", "ls": "dotted", "label": "Adjacent-Layer"},
    "proposed_without_batch": {"color": PROPOSED_GREEN, "marker": "circle", "ls": "dotted", "label": "Proposed w/o Batch"},
    "srd": {"color": ABLATION_GRAY, "marker": "square", "ls": "dotted", "label": "SRD"},
    "netkeeper": {"color": ABLATION_GRAY, "marker": "square", "ls": "dotted", "label": "NetKeeper*"},
}

# Fallback for any method_id not explicitly registered above.
_FALLBACK_STYLE = {"color": EXTRA_PURPLE, "marker": "circle", "ls": "dotted", "label": "Unknown"}


def style_for(method_id: str) -> dict:
    spec = METHOD_STYLE.get(method_id, _FALLBACK_STYLE)
    return {
        "color": spec["color"],
        "marker": MARKERS[spec["marker"]],
        "linestyle": LINESTYLES[spec["ls"]],
        "label": spec["label"],
    }


# Categorical x-axis labels.
CATEGORICAL_X = {
    "exp1": {  # exp1(b) success-rate bar: x = method
        0: "Proposed",
        1: "CSPF",
        2: "SFC Re-opt",
    },
    "exp4": {  # exp4 recovery: x = failure type
        0: "Link",
        1: "Agent",
        2: "Capacity",
    },
}

# (experiment, subplot) -> (title, xlabel, ylabel, y_percent, kind)
# kind in {"line", "bar", "scatter", "grouped_bar"}
SUBFIGURE_META = {
    ("exp1", "a"): ("Formation Latency", "Task Size (agents)", "E2E Formation (ms)", False, "line"),
    ("exp1", "b"): ("Formation Success Rate", "Method", "Success Rate (%)", True, "bar"),
    ("exp2", "a"): ("QoS Satisfaction", "Conflict Density (%)", "QoS Satisfaction (%)", True, "line"),
    ("exp2", "b"): ("Safe Rejection Rate", "Conflict Density (%)", "Safe Rejection (%)", True, "line"),
    ("exp3", "a"): ("Reconfiguration Latency", "Changed Agents", "Latency (ms)", False, "line"),
    ("exp3", "b"): ("Scope vs Success Tradeoff", "Modification Scope (%)", "Success Rate (%)", True, "scatter"),
    ("exp4", "a"): ("Recovery Latency", "Failure Type", "Recovery (ms)", False, "grouped_bar"),
    ("exp4", "b"): ("Modification Scope", "Failure Type", "Changed Scope (%)", True, "grouped_bar"),
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
    """Plot Proposed first, then the rest sorted for stable legend ordering."""
    unique = set(methods)
    order = ["proposed"] if "proposed" in unique else []
    order.extend(sorted(unique - set(order)))
    return order


def _categorical_ticks(ax, experiment: str, categories) -> None:
    if experiment in CATEGORICAL_X:
        labels = {int(k): v for k, v in CATEGORICAL_X[experiment].items()}
        ticks = sorted(labels.keys())
        ax.set_xticks(ticks)
        ax.set_xticklabels([labels[t] for t in ticks])
    else:
        ax.set_xticks(sorted(categories))


def _plot_line(ax, rows, experiment, subplot):
    title, xlabel, ylabel, is_pct, _ = SUBFIGURE_META[(experiment, subplot)]
    methods = _ordered_methods({r["method"] for r in rows})
    by_method = {m: [] for m in methods}
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
    if experiment in CATEGORICAL_X:
        _categorical_ticks(ax, experiment, {int(r["x"]) for r in rows})
    ax.set_title(title, fontsize=10)
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    if is_pct:
        ax.set_ylim(0, 105)
    ax.legend(fontsize=8, frameon=False, loc="best")


def _plot_scatter(ax, rows, experiment, subplot):
    title, xlabel, ylabel, is_pct, _ = SUBFIGURE_META[(experiment, subplot)]
    methods = _ordered_methods({r["method"] for r in rows})
    for m in methods:
        xs = [r["x"] for r in rows if r["method"] == m]
        ys = [r["y_mean"] for r in rows if r["method"] == m]
        st = style_for(m)
        scatter_kwargs = dict(
            color=st["color"], marker=st["marker"],
            label=st["label"], s=42, linewidths=1.0,
        )
        if st["marker"] not in ("x", "+"):  # unfilled markers ignore edgecolors
            scatter_kwargs["edgecolors"] = "white"
        ax.scatter(xs, ys, **scatter_kwargs)
    ax.set_title(title, fontsize=10)
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    if is_pct:
        ax.set_ylim(0, 105)
    ax.legend(fontsize=8, frameon=False, loc="best")


def _plot_grouped_bar(ax, rows, experiment, subplot):
    title, xlabel, ylabel, is_pct, _ = SUBFIGURE_META[(experiment, subplot)]
    categories = sorted({int(r["x"]) for r in rows})
    # Per-category method set so bars are centred within each group.
    methods_by_cat: dict[int, list[str]] = {c: [] for c in categories}
    for r in rows:
        c = int(r["x"])
        if r["method"] not in methods_by_cat[c]:
            methods_by_cat[c].append(r["method"])
    for c in categories:
        methods_by_cat[c] = _ordered_methods(methods_by_cat[c])

    value = {
        (int(r["x"]), r["method"]): r for r in rows
    }
    for c in categories:
        methods = methods_by_cat[c]
        n = len(methods)
        width = 0.8 / max(n, 1)
        for j, m in enumerate(methods):
            center = c + (j - (n - 1) / 2) * width
            row = value.get((c, m))
            if row is None:
                continue
            st = style_for(m)
            ax.bar(
                center, row["y_mean"], width,
                color=st["color"], label=st["label"],
                edgecolor="white", linewidth=0.6,
                yerr=[[row["y_mean"] - row["y_lo"]], [row["y_hi"] - row["y_mean"]]],
                capsize=2, error_kw={"elinewidth": 0.8, "ecolor": "#555555"},
            )
    _categorical_ticks(ax, experiment, set(categories))
    ax.set_title(title, fontsize=10)
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    if is_pct:
        ax.set_ylim(0, 105)
    ax.legend(fontsize=8, frameon=False, loc="best")


def _plot_subfigure(ax, rows, experiment, subplot):
    kind = SUBFIGURE_META[(experiment, subplot)][4]
    if kind == "line":
        _plot_line(ax, rows, experiment, subplot)
    elif kind == "scatter":
        _plot_scatter(ax, rows, experiment, subplot)
    else:  # "bar" and "grouped_bar" share the grouped-bar renderer
        _plot_grouped_bar(ax, rows, experiment, subplot)


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

    # Exp1: formation latency rises with task size; ordering
    # proposed < CSPF < SFC-Reopt (SFC-Reopt ~1.5x CSPF, never "≈").
    sizes = [8, 12, 16, 20, 24, 28, 32]
    base = {"proposed": 500, "cspf": 1600, "sfc_reoptimization": 2400}
    slope = {"proposed": 35, "cspf": 260, "sfc_reoptimization": 390}
    for m, b in base.items():
        for x in sizes:
            y = b + slope[m] * (x - 8)
            rows.append({"experiment": "exp1", "subplot": "a", "method": m, "x": x,
                         "y_mean": y, "y_lo": y - 60, "y_hi": y + 60})
    # Exp1(b): success-rate bar (x = method index 0/1/2).
    sr = {"proposed": 99.5, "cspf": 98.4, "sfc_reoptimization": 98.8}
    for i, m in enumerate(("proposed", "cspf", "sfc_reoptimization")):
        rows.append({"experiment": "exp1", "subplot": "b", "method": m, "x": i,
                     "y_mean": sr[m], "y_lo": sr[m] - 1.2, "y_hi": sr[m] + 1.2})

    # Exp2: conflict density 0..60.
    #   Low conflict: all similar. High conflict: independent degrades most;
    #   weighted-sum better but may violate constraints; SANet better semantic;
    #   proposed highest because of hard verification.
    densities = [0, 10, 20, 30, 40, 50, 60]
    qos = {
        "proposed": lambda d: 99 - 0.05 * d,
        "sanet_dw": lambda d: 98 - 0.30 * d,
        "weighted_sum": lambda d: 97 - 0.85 * d,
        "independent": lambda d: 96 - 1.6 * d,
    }
    safe = {  # unsafe configurations DETECTED instead of executed
        "proposed": lambda d: 99 - 0.04 * d,
        "sanet_dw": lambda d: 93 - 0.25 * d,
        "weighted_sum": lambda d: 82 - 0.9 * d,  # accepts some infeasible
        "independent": lambda d: 60 - 1.3 * d,    # no cross-layer check
    }
    for m in qos:
        for d in densities:
            yq = max(20, qos[m](d) + jitter(1.2))
            rows.append({"experiment": "exp2", "subplot": "a", "method": m, "x": d,
                         "y_mean": yq, "y_lo": yq - 2.5, "y_hi": yq + 2.5})
            ys = max(10, safe[m](d) + jitter(2.0))
            rows.append({"experiment": "exp2", "subplot": "b", "method": m, "x": d,
                         "y_mean": ys, "y_lo": ys - 3.0, "y_hi": ys + 3.0})

    # Exp3: changed agents; proposed small scope + high success.
    ncs = [1, 2, 4, 8]
    scope_of = {
        "proposed": lambda x: 8 + 3 * x,
        "full_rebuild": lambda x: 95 + 2 * x,
        "local_only": lambda x: 4 + x,
        "netren": lambda x: 40 + 8 * x,
    }
    succ_of = {
        "proposed": lambda x: 99 - 0.4 * x,
        "full_rebuild": lambda x: 99,
        "local_only": lambda x: 70 + 3 * x,    # misses global dependency
        "netren": lambda x: 90 + 1 * x,
    }
    for m in scope_of:
        for x in ncs:
            lat = {"proposed": 300 + 40 * x, "full_rebuild": 1200 + 200 * x}.get(
                m, 700 + 90 * x
            )
            rows.append({"experiment": "exp3", "subplot": "a", "method": m, "x": x,
                         "y_mean": lat, "y_lo": lat - 80, "y_hi": lat + 80})
            scope = scope_of[m](x)
            succ = succ_of[m](x)
            rows.append({"experiment": "exp3", "subplot": "b", "method": m, "x": scope,
                         "y_mean": succ, "y_lo": succ - 3, "y_hi": succ + 3})

    # Exp4: failure types 0=Link,1=Agent,2=Capacity.
    # Per-failure method sets exactly match the frozen protocol:
    #   Link     : proposed, cspf
    #   Agent    : proposed, sfc_restoration, full_rebuild
    #   Capacity : proposed, te_reopt, full_rebuild
    # Expected scientific story (no forced win):
    #   Link     -> routing methods competitive (proposed ~ cspf)
    #   Agent    -> cross-layer awareness wins (proposed < sfc_restoration < full_rebuild)
    #   Capacity -> semantic scope control avoids needless global change
    #              (proposed < te_reopt < full_rebuild)
    recovery = {
        "proposed": {"Link": 900, "Agent": 700, "Capacity": 800},
        "cspf": {"Link": 1050},
        "sfc_restoration": {"Agent": 1500},
        "full_rebuild": {"Agent": 2400, "Capacity": 2200},
        "te_reopt": {"Capacity": 1500},
    }
    scope = {
        "proposed": {"Link": 20, "Agent": 18, "Capacity": 22},
        "cspf": {"Link": 48},
        "sfc_restoration": {"Agent": 70},
        "full_rebuild": {"Agent": 95, "Capacity": 92},
        "te_reopt": {"Capacity": 40},
    }
    for m, per_ft in recovery.items():
        for ft, lat in per_ft.items():
            xi = {"Link": 0, "Agent": 1, "Capacity": 2}[ft]
            rows.append({"experiment": "exp4", "subplot": "a", "method": m, "x": xi,
                         "y_mean": lat, "y_lo": lat - 90, "y_hi": lat + 90})
            sc = scope[m][ft]
            rows.append({"experiment": "exp4", "subplot": "b", "method": m, "x": xi,
                         "y_mean": sc, "y_lo": sc - 5, "y_hi": sc + 5})
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
