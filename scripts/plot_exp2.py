from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

from PIL import Image, ImageDraw, ImageFont


COLORS = {
    "proposed": "#2563EB",
    "independent": "#DC2626",
    "adjacent": "#16A34A",
    "no_verification": "#9333EA",
    "without_application": "#F59E0B",
    "without_transport": "#06B6D4",
    "without_network": "#EC4899",
    "without_physical": "#64748B",
}
SCENARIO_PREFIX = {
    "application_capacity": "A",
    "transport_network": "T",
    "network_physical": "N",
}


def plot_exp2(
    summary_csv: Path,
    timeline_csv: Path,
    output_dir: Path,
) -> list[Path]:
    summary = _read_csv(summary_csv)
    timeline = _read_csv(timeline_csv)
    if not summary:
        raise ValueError("experiment-2 summary is empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    definitions = (
        (
            "fig_qos_satisfaction_vs_conflict_pressure",
            "qos_satisfaction_rate",
            "QoS satisfaction rate",
        ),
        (
            "fig_infeasible_rate_vs_conflict_pressure",
            "infeasible_configuration_rate",
            "Infeasible configuration rate",
        ),
        (
            "fig_conflict_detection_rate",
            "conflict_detection_rate",
            "Conflict detection rate",
        ),
        (
            "fig_conflict_resolution_rate",
            "conflict_resolution_rate",
            "Conflict resolution rate",
        ),
        (
            "fig_false_alarm_rate",
            "false_alarm_rate",
            "False alarm rate",
        ),
        (
            "fig_coordination_latency",
            "coordination_latency_ms",
            "Coordination latency (ms)",
        ),
        (
            "fig_coordination_overhead",
            "control_bytes",
            "Control-plane bytes",
        ),
    )
    rate_metrics = {
        "qos_satisfaction_rate",
        "infeasible_configuration_rate",
        "conflict_detection_rate",
        "conflict_resolution_rate",
        "false_alarm_rate",
    }
    for name, metric, y_label in definitions:
        rows = _main_metric_rows(summary, metric, metric in rate_metrics)
        csv_path = output_dir / f"{name}.csv"
        _write_rows(csv_path, rows)
        outputs.append(csv_path)
        outputs.extend(
            _line_chart(
                output_dir / name,
                rows,
                title=name.removeprefix("fig_").replace("_", " ").title(),
                x_label="Conflict pressure level (A/T/N scenarios)",
                y_label=y_label,
                separators=(6, 12),
            )
        )

    ablation = _ablation_rows(summary)
    ablation_name = "fig_layer_ablation"
    ablation_csv = output_dir / f"{ablation_name}.csv"
    _write_rows(ablation_csv, ablation)
    outputs.append(ablation_csv)
    outputs.extend(
        _bar_chart(
            output_dir / ablation_name,
            ablation,
            title="Four-Layer Ablation",
            y_label="QoS satisfaction rate",
        )
    )

    compound = _compound_rows(timeline)
    compound_name = "fig_compound_scenario_timeline"
    compound_csv = output_dir / f"{compound_name}.csv"
    _write_rows(compound_csv, compound)
    outputs.append(compound_csv)
    outputs.extend(
        _compound_chart(
            output_dir / compound_name,
            compound,
            title="Four-Layer Compound Scenario Timeline",
        )
    )
    return outputs


def _main_metric_rows(
    summary: list[dict[str, str]],
    metric: str,
    direct_rate: bool,
) -> list[dict[str, object]]:
    scenario_order = list(SCENARIO_PREFIX)
    pressure_values = {
        scenario: sorted(
            {
                float(row["conflict_pressure"])
                for row in summary
                if row["scenario_family"] == scenario
            }
        )
        for scenario in scenario_order
    }
    output = []
    for row in summary:
        scenario = row["scenario_family"]
        if scenario not in SCENARIO_PREFIX or row["method"] not in COLORS:
            continue
        pressure = float(row["conflict_pressure"])
        level = pressure_values[scenario].index(pressure) + 1
        if direct_rate:
            mean = float(row[metric])
            ci95 = float(row.get(f"{metric}_ci95", 0.0) or 0.0)
        else:
            mean = float(row[f"{metric}_mean"])
            ci95 = float(row.get(f"{metric}_ci95", 0.0) or 0.0)
        output.append(
            {
                "x": f"{SCENARIO_PREFIX[scenario]}{level}",
                "scenario": scenario,
                "pressure": pressure,
                "pressure_level": level,
                "method": row["method"],
                "mean": mean,
                "ci95": ci95,
                "sample_count": int(row["runs"]),
            }
        )
    return sorted(
        output,
        key=lambda row: (
            scenario_order.index(str(row["scenario"])),
            int(row["pressure_level"]),
            str(row["method"]),
        ),
    )


def _ablation_rows(summary: list[dict[str, str]]) -> list[dict[str, object]]:
    order = (
        "proposed",
        "without_application",
        "without_transport",
        "without_network",
        "without_physical",
    )
    rows = [
        {
            "method": row["method"],
            "mean": float(row["qos_satisfaction_rate"]),
            "ci95": float(row.get("qos_satisfaction_rate_ci95", 0.0) or 0.0),
            "success_rate": float(row["success_rate"]),
            "infeasible_configuration_rate": float(
                row["infeasible_configuration_rate"]
            ),
            "sample_count": int(row["runs"]),
        }
        for row in summary
        if row["scenario"] == "ablation" and row["method"] in order
    ]
    return sorted(rows, key=lambda row: order.index(str(row["method"])))


def _compound_rows(timeline: list[dict[str, str]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in timeline:
        groups[(row["method"], int(row["time_step"]))].append(row)
    output = []
    for (method, step), rows in sorted(groups.items()):
        output.append(
            {
                "method": method,
                "time_step": step,
                "pressure": fmean(float(row["pressure"]) for row in rows),
                "application_demand_mbps": fmean(
                    float(row["application_demand_mbps"]) for row in rows
                ),
                "network_available_bandwidth_mbps": fmean(
                    float(row["network_available_bandwidth_mbps"]) for row in rows
                ),
                "physical_available_capacity_mbps": fmean(
                    float(row["physical_available_capacity_mbps"]) for row in rows
                ),
                "actual_send_rate_mbps": fmean(
                    float(row["actual_send_rate_mbps"]) for row in rows
                ),
                "qos_satisfaction_rate": fmean(
                    _bool(row["qos_satisfied"]) for row in rows
                ),
                "conflict_detection_rate": fmean(
                    _bool(row["conflict_detected"]) for row in rows
                ),
                "transaction_success_rate": fmean(
                    _bool(row["transaction_success"]) for row in rows
                ),
                "sample_count": len(rows),
            }
        )
    return output


def _line_chart(
    base_path: Path,
    rows: list[dict[str, object]],
    *,
    title: str,
    x_label: str,
    y_label: str,
    separators: tuple[int, ...] = (),
) -> list[Path]:
    image, draw, font, small = _canvas(title)
    plot = (120, 90, 1340, 790)
    _axes(draw, plot, x_label, y_label, font, small)
    x_values = list(dict.fromkeys(str(row["x"]) for row in rows))
    maximum = max(
        (float(row["mean"]) + float(row.get("ci95", 0.0)) for row in rows),
        default=1.0,
    )
    maximum = max(1.0 if maximum <= 1.0 else maximum * 1.12, 1e-9)
    _y_ticks(draw, plot, maximum, small)
    positions = _x_positions(plot, len(x_values))
    for label, x in zip(x_values, positions):
        draw.text((x - 10, plot[3] + 10), label, fill="black", font=small)
    for index in separators:
        if 0 < index < len(positions):
            x = (positions[index - 1] + positions[index]) // 2
            draw.line((x, plot[1], x, plot[3]), fill="#CBD5E1", width=2)
    grouped: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for row in rows:
        grouped[str(row["method"])][str(row["x"])] = row
    for method, by_x in sorted(grouped.items()):
        color = COLORS.get(method, "#64748B")
        points = []
        for label, x in zip(x_values, positions):
            row = by_x.get(label)
            if row is None:
                continue
            mean = float(row["mean"])
            ci = float(row.get("ci95", 0.0))
            y = _map_y(mean, plot, maximum)
            low = _map_y(max(0.0, mean - ci), plot, maximum)
            high = _map_y(mean + ci, plot, maximum)
            draw.line((x, low, x, high), fill=color, width=2)
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color)
            points.append((x, y))
        if len(points) > 1:
            draw.line(points, fill=color, width=3)
    _legend(draw, list(grouped), font)
    return _save(image, base_path)


def _bar_chart(
    base_path: Path,
    rows: list[dict[str, object]],
    *,
    title: str,
    y_label: str,
) -> list[Path]:
    image, draw, font, small = _canvas(title)
    plot = (120, 90, 1340, 790)
    _axes(draw, plot, "Ablation", y_label, font, small)
    _y_ticks(draw, plot, 1.0, small)
    positions = _x_positions(plot, len(rows))
    width = max(30, min(90, 700 // max(1, len(rows))))
    for row, x in zip(rows, positions):
        value = float(row["mean"])
        ci = float(row.get("ci95", 0.0))
        y = _map_y(value, plot, 1.0)
        color = COLORS.get(str(row["method"]), "#64748B")
        draw.rectangle((x - width, y, x + width, plot[3]), fill=color)
        low = _map_y(max(0.0, value - ci), plot, 1.0)
        high = _map_y(min(1.0, value + ci), plot, 1.0)
        draw.line((x, low, x, high), fill="black", width=2)
        label = str(row["method"]).replace("without_", "w/o ")
        draw.text((x - width, plot[3] + 12), label, fill="black", font=small)
    return _save(image, base_path)


def _compound_chart(
    base_path: Path,
    rows: list[dict[str, object]],
    *,
    title: str,
) -> list[Path]:
    image = Image.new("RGB", (1500, 1000), "white")
    draw = ImageDraw.Draw(image)
    font = _font(22)
    small = _font(16)
    draw.text((40, 20), title, fill="black", font=_font(30))
    methods = ["proposed", "independent", "adjacent", "no_verification"]
    metric_colors = {
        "application_demand_mbps": "#2563EB",
        "actual_send_rate_mbps": "#9333EA",
        "network_available_bandwidth_mbps": "#16A34A",
        "physical_available_capacity_mbps": "#DC2626",
    }
    for index, method in enumerate(methods):
        left = 80 + (index % 2) * 720
        top = 90 + (index // 2) * 430
        plot = (left, top, left + 620, top + 330)
        draw.rectangle(plot, outline="#334155", width=2)
        draw.text((left + 10, top + 8), method, fill="black", font=font)
        method_rows = sorted(
            (row for row in rows if row["method"] == method),
            key=lambda row: int(row["time_step"]),
        )
        maximum = max(
            (
                float(row[metric])
                for row in method_rows
                for metric in metric_colors
            ),
            default=1.0,
        ) * 1.12
        inner = (left + 55, top + 45, left + 590, top + 280)
        xs = _x_positions(inner, len(method_rows))
        for metric, color in metric_colors.items():
            points = [
                (x, _map_y(float(row[metric]), inner, maximum))
                for x, row in zip(xs, method_rows)
            ]
            if len(points) > 1:
                draw.line(points, fill=color, width=3)
            for point in points:
                draw.ellipse(
                    (point[0] - 3, point[1] - 3, point[0] + 3, point[1] + 3),
                    fill=color,
                )
        for x, row in zip(xs, method_rows):
            draw.text((x - 5, inner[3] + 6), str(row["time_step"]), fill="black", font=small)
            qos_color = "#16A34A" if float(row["qos_satisfaction_rate"]) >= 0.5 else "#DC2626"
            draw.rectangle((x - 5, inner[3] + 28, x + 5, inner[3] + 38), fill=qos_color)
            if float(row["conflict_detection_rate"]) > 0.0:
                draw.line((x, inner[1], x, inner[3]), fill="#F59E0B", width=2)
    legend_y = 950
    for index, (metric, color) in enumerate(metric_colors.items()):
        x = 60 + index * 340
        draw.line((x, legend_y, x + 35, legend_y), fill=color, width=4)
        draw.text((x + 42, legend_y - 10), metric.replace("_mbps", "").replace("_", " "), fill="black", font=small)
    return _save(image, base_path)


def _canvas(title: str):
    image = Image.new("RGB", (1450, 900), "white")
    draw = ImageDraw.Draw(image)
    title_font = _font(30)
    font = _font(20)
    small = _font(15)
    draw.text((100, 25), title, fill="black", font=title_font)
    return image, draw, font, small


def _font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _axes(draw, plot, x_label, y_label, font, small):
    draw.line((plot[0], plot[3], plot[2], plot[3]), fill="black", width=2)
    draw.line((plot[0], plot[1], plot[0], plot[3]), fill="black", width=2)
    draw.text(((plot[0] + plot[2]) // 2 - 150, plot[3] + 55), x_label, fill="black", font=font)
    draw.text((5, plot[1] - 25), y_label, fill="black", font=small)


def _y_ticks(draw, plot, maximum, font):
    for index in range(6):
        value = maximum * index / 5
        y = _map_y(value, plot, maximum)
        draw.line((plot[0] - 5, y, plot[2], y), fill="#E2E8F0", width=1)
        draw.text((plot[0] - 78, y - 8), f"{value:.2g}", fill="black", font=font)


def _map_y(value, plot, maximum):
    return int(plot[3] - value / max(maximum, 1e-12) * (plot[3] - plot[1]))


def _x_positions(plot, count):
    if count <= 1:
        return [(plot[0] + plot[2]) // 2]
    margin = 25
    return [
        int(plot[0] + margin + index * (plot[2] - plot[0] - 2 * margin) / (count - 1))
        for index in range(count)
    ]


def _legend(draw, methods: list[str], font):
    for index, method in enumerate(methods):
        x = 850 + (index % 2) * 260
        y = 30 + (index // 2) * 30
        color = COLORS.get(method, "#64748B")
        draw.line((x, y + 8, x + 35, y + 8), fill=color, width=4)
        draw.text((x + 45, y), method, fill="black", font=font)


def _save(image: Image.Image, base_path: Path) -> list[Path]:
    png = base_path.with_suffix(".png")
    pdf = base_path.with_suffix(".pdf")
    image.save(png, format="PNG")
    image.save(pdf, format="PDF", resolution=150.0)
    return [png, pdf]


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"plot data is empty: {path.name}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _bool(value: object) -> float:
    return 1.0 if str(value).strip().lower() in {"1", "true", "yes"} else 0.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plot experiment 2")
    parser.add_argument("--summary", default="results/exp2/processed/summary.csv")
    parser.add_argument("--timeline", default="results/exp2/raw/compound_timeline.csv")
    parser.add_argument("--output-dir", default="results/exp2/figures")
    return parser


def main() -> None:
    args = _parser().parse_args()
    outputs = plot_exp2(Path(args.summary), Path(args.timeline), Path(args.output_dir))
    print(f"Wrote {len(outputs)} experiment-2 figure/data artifacts")


if __name__ == "__main__":
    main()
