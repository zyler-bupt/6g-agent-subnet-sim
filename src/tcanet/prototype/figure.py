"""SANet-style prototype figure: labelled window captures over a desk photo."""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import ConnectionPatch  # noqa: E402
from PIL import Image  # noqa: E402

from src.tcanet.prototype import config  # noqa: E402

PANELS = (("controller", "Agent Controller"), ("app_dag", "AppAgent"),
          ("flows", "TransAgent / NetAgent"), ("phy", "PhyAgent"))


def parse_anchors(text: str | None) -> list[tuple[float, float]] | None:
    """``"x,y;x,y;..."`` in photo fractions (0..1, origin top-left)."""
    if not text:
        return None
    return [tuple(float(v) for v in pair.split(",")) for pair in text.split(";")]


def compose(captures: Path, photo: Path | None, out: Path,
            anchors: list[tuple[float, float]] | None = None) -> Path:
    fig = plt.figure(figsize=(12, 7.2))
    grid = fig.add_gridspec(2, len(PANELS), height_ratios=(1.0, 1.25), hspace=0.18, wspace=0.06)
    panel_axes = []
    for index, (name, label) in enumerate(PANELS):
        ax = fig.add_subplot(grid[0, index])
        path = Path(captures) / f"{name}.png"
        if path.exists():
            ax.imshow(Image.open(path))
        else:
            ax.text(0.5, 0.5, f"missing\n{path.name}", ha="center", va="center", color="#898781")
        ax.set_title(label, fontsize=11, fontweight="bold")
        ax.set_xticks([])
        ax.set_yticks([])
        panel_axes.append(ax)
    photo_ax = fig.add_subplot(grid[1, :])
    photo_ax.set_xticks([])
    photo_ax.set_yticks([])
    if photo is not None and Path(photo).exists():
        photo_ax.imshow(Image.open(photo))
    else:
        photo_ax.text(0.5, 0.5, "desk photo (Ubuntu host running the prototype)",
                      ha="center", va="center", color="#898781", transform=photo_ax.transAxes)
    anchors = anchors or [((i + 0.5) / len(PANELS), 0.3) for i in range(len(PANELS))]
    for ax, (x, y) in zip(panel_axes, anchors):
        fig.add_artist(ConnectionPatch(
            xyA=(0.5, 0.0), coordsA=ax.transAxes, xyB=(x, 1.0 - y), coordsB=photo_ax.transAxes,
            arrowstyle="-|>", color="#2a78d6", linewidth=1.4))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="SANet-style TCANet prototype figure")
    parser.add_argument("--captures", default=str(config.captures_dir() / "recovered"))
    parser.add_argument("--photo")
    parser.add_argument("--anchors", help='photo points per panel, e.g. "0.2,0.4;0.4,0.4;0.6,0.4;0.8,0.4"')
    parser.add_argument("--out", default=str(config.run_dir() / "prototype_figure.pdf"))
    args = parser.parse_args()
    print(compose(Path(args.captures), Path(args.photo) if args.photo else None, Path(args.out),
                  parse_anchors(args.anchors)))


if __name__ == "__main__":
    main()
