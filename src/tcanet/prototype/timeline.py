"""Recovery timeline figure from a run's ``events.jsonl`` (paper style, light).

Two stacked panels on one time axis — goodput and one-way delay per
dependency — with vertical markers for operator faults, controller event
detection, rollbacks and commits.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from src.tcanet.prototype import config  # noqa: E402

SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a")
MARKERS = {"ops": ("#898781", ":"), "event": ("#d03b3b", "--"), "rollback": ("#c98500", "--"),
           "commit": ("#0ca30c", "-")}


def load_events(path: Path) -> list[dict]:
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def build_series(events: list[dict], start: float | None = None, end: float | None = None) -> dict:
    flows = [e for e in events if e["topic"] == "report.flow"]
    t0 = start if start is not None else (flows[0]["ts"] if flows else 0.0)
    rx: dict[str, list[tuple[float, float]]] = {}
    owd: dict[str, list[tuple[float, float]]] = {}
    markers: list[tuple[float, str, str]] = []
    for event in events:
        ts = float(event["ts"])
        if ts < t0 or (end is not None and ts > end):
            continue
        t, p = ts - t0, event["payload"]
        if event["topic"] == "report.flow":
            rx.setdefault(p["dep_id"], []).append((t, p["rx_mbps"]))
            if p.get("owd_ms") is not None:
                owd.setdefault(p["dep_id"], []).append((t, p["owd_ms"]))
        elif event["topic"] == "ops.fault" and p["op"] in ("gateway", "link", "degrade", "kill", "demand"):
            markers.append((t, f"{p['op']} {p.get('target', p.get('dep_id', ''))}", "ops"))
        elif event["topic"] == "ctrl.log" and p["step"] == "event":
            markers.append((t, "detected", "event"))
        elif event["topic"] == "ctrl.log" and p["step"] == "rollback" and p["title"].startswith("Rollback"):
            markers.append((t, "rollback", "rollback"))
        elif event["topic"] == "ctrl.log" and p["step"] == "commit" and p["level"] == "ok":
            markers.append((t, p["title"].replace("Commit ", "commit "), "commit"))
    return {"rx": rx, "owd": owd, "markers": markers}


def _with_gaps(data: list[tuple[float, float]], gap_s: float = 1.5) -> tuple[list[float], list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    for t, v in data:
        if xs and t - xs[-1] > gap_s:
            xs.append(xs[-1])
            ys.append(float("nan"))
        xs.append(t)
        ys.append(v)
    return xs, ys


def plot(series: dict, out: Path) -> Path:
    fig, (ax_rx, ax_owd) = plt.subplots(2, 1, figsize=(7.2, 4.6), sharex=True)
    for index, dep in enumerate(sorted(series["rx"])):
        color = SERIES_LIGHT[index % len(SERIES_LIGHT)]
        for ax, data in ((ax_rx, series["rx"].get(dep, [])), (ax_owd, series["owd"].get(dep, []))):
            if data:
                xs, ys = _with_gaps(data)
                ax.plot(xs, ys, color=color, linewidth=1.6, label=dep)
                ax.annotate(dep, (xs[-1], ys[-1]), xytext=(4, 0), textcoords="offset points",
                            color="#52514e", fontsize=8, va="center")
    for index, (t, label, kind) in enumerate(series["markers"]):
        color, style = MARKERS[kind]
        for ax in (ax_rx, ax_owd):
            ax.axvline(t, color=color, linestyle=style, linewidth=1.0)
        ax_rx.annotate(label, (t, 1.0 - 0.2 * (index % 3)), xycoords=("data", "axes fraction"),
                       rotation=90, fontsize=7, color="#52514e", va="top", ha="right")
    ax_rx.set_ylabel("Goodput (Mbps)")
    ax_owd.set_ylabel("One-way delay (ms)")
    ax_owd.set_xlabel("Time (s)")
    for ax in (ax_rx, ax_owd):
        ax.grid(axis="y", color="#e1e0d9", linewidth=0.6)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    handles, labels = ax_rx.get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, fontsize=8, frameon=False,
               bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200)
    plt.close(fig)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet recovery timeline figure")
    parser.add_argument("--events", default=str(config.events_path()))
    parser.add_argument("--out", default=str(config.run_dir() / "timeline.pdf"))
    parser.add_argument("--start", type=float, help="epoch seconds to start from")
    parser.add_argument("--end", type=float)
    args = parser.parse_args()
    out = plot(build_series(load_events(Path(args.events)), args.start, args.end), Path(args.out))
    print(out)


if __name__ == "__main__":
    main()
