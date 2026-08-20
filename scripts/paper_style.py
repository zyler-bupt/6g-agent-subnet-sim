from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

_MPL_CACHE = Path(tempfile.gettempdir()) / "6g-agent-subnet-matplotlib"
_MPL_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE))

import matplotlib as mpl

mpl.use("Agg")


@dataclass(frozen=True)
class MethodStyle:
    label: str
    color: str
    linestyle: str | tuple[int, tuple[float, ...]]
    marker: str
    hatch: str = ""


METHOD_STYLES = {
    "proposed": MethodStyle("Proposed", "#1F4E79", "-", "o", ""),
    "proposed_without_batch": MethodStyle(
        "Proposed w/o Batch", "#5B9BD5", "--", "o", "//"
    ),
    "a1_agent_embedded": MethodStyle(
        "A1-Agent-Embedded*", "#7F7F7F", ":", "^", ".."
    ),
    "sanet_dw": MethodStyle("SANet-DW*", "#ED7D31", "-.", "D", "xx"),
    "adjacent_layer": MethodStyle(
        "Adjacent-Layer", "#70ADCF", "--", "^", "//"
    ),
    "independent": MethodStyle("Independent", "#7F7F7F", ":", "X", ".."),
    "netren": MethodStyle("NetRen*", "#ED7D31", "-.", "D", "xx"),
    "local_only": MethodStyle("Local-Only", "#8064A2", "--", "v", "//"),
    "full_rebuild": MethodStyle("Full-Rebuild", "#A61C2D", ":", "X", "xx"),
    "netkeeper": MethodStyle("NetKeeper*", "#ED7D31", "-.", "D", "xx"),
    "ours": MethodStyle("Ours", "#1F4E79", "-", "o", ""),
    "full_reconfiguration": MethodStyle(
        "Full Reconfiguration", "#C55A11", "--", "s", r"\\"
    ),
    "alc": MethodStyle(
        "Adjacent-Layer Coordination (ALC)", "#548235", "-.", "^", ".."
    ),
    "layer_wise_independent": MethodStyle(
        "Layer-wise Independent", "#595959", ":", "D", "xx"
    ),
    "no_global_verification": MethodStyle(
        "w/o Global Verification", "#8064A2", (0, (3.0, 1.5)), "v", "--"
    ),
    "cspf": MethodStyle("CSPF", "#548235", "-.", "s", "//"),
}

LINEWIDTH = 1.7
MARKERSIZE = 4.5
CI_ALPHA = 0.15


def apply_paper_style() -> None:
    mpl.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["Times New Roman", "Liberation Serif", "DejaVu Serif"],
            "font.size": 8.0,
            "axes.labelsize": 9.0,
            "axes.titlesize": 9.0,
            "axes.linewidth": 0.85,
            "axes.edgecolor": "#333333",
            "axes.facecolor": "white",
            "figure.facecolor": "white",
            "xtick.labelsize": 8.0,
            "ytick.labelsize": 8.0,
            "xtick.major.width": 0.8,
            "ytick.major.width": 0.8,
            "xtick.major.size": 3.0,
            "ytick.major.size": 3.0,
            "legend.fontsize": 8.0,
            "legend.frameon": False,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "savefig.facecolor": "white",
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.03,
            "hatch.linewidth": 0.7,
        }
    )


def style_axis(axis, *, horizontal_grid: bool = True) -> None:
    for spine in axis.spines.values():
        spine.set_linewidth(0.85)
        spine.set_color("#333333")
    axis.grid(
        horizontal_grid,
        axis="y",
        color="#777777",
        linestyle="--",
        linewidth=0.5,
        alpha=0.18,
    )
    axis.grid(False, axis="x")
    axis.set_axisbelow(True)


def panel_label(axis, label: str) -> None:
    axis.text(
        -0.13,
        1.035,
        label,
        transform=axis.transAxes,
        fontsize=9.0,
        fontweight="bold",
        va="bottom",
        ha="left",
        clip_on=False,
    )


def method_line_kwargs(method_key: str) -> dict[str, object]:
    style = METHOD_STYLES[method_key]
    return {
        "label": style.label,
        "color": style.color,
        "linestyle": style.linestyle,
        "marker": style.marker,
        "linewidth": LINEWIDTH,
        "markersize": MARKERSIZE,
        "markerfacecolor": "white",
        "markeredgecolor": style.color,
        "markeredgewidth": 0.9,
        "zorder": 3,
    }


def method_bar_kwargs(method_key: str) -> dict[str, object]:
    style = METHOD_STYLES[method_key]
    return {
        "label": style.label,
        "color": style.color,
        "edgecolor": "#222222",
        "linewidth": 0.9,
        "hatch": style.hatch,
        "zorder": 3,
    }
