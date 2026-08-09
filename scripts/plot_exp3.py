from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


COLORS = {
    "proposed": "#2563EB",
    "full_rebuild": "#DC2626",
    "local_only": "#16A34A",
}


def plot_exp3(summary_csv: Path, output_dir: Path) -> list[Path]:
    with summary_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("experiment 3 summary CSV is empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    definitions = (
        (
            "fig_elastic_latency_vs_agent_count",
            "agent_count",
            "elastic_latency_ms",
            "Business Agent count",
            "Elastic latency (ms)",
        ),
        (
            "fig_elastic_latency_vs_removal_ratio",
            "removal_ratio",
            "elastic_latency_ms",
            "Agent removal ratio",
            "Elastic latency (ms)",
        ),
        (
            "fig_elastic_latency_vs_gateway_count",
            "gateway_count",
            "elastic_latency_ms",
            "Gateway count",
            "Elastic latency (ms)",
        ),
        (
            "fig_changed_rules_ratio",
            "removal_ratio",
            "changed_rules_ratio",
            "Agent removal ratio",
            "Changed rules / original rules",
        ),
        (
            "fig_affected_gateways",
            "removal_ratio",
            "affected_gateways",
            "Agent removal ratio",
            "Affected gateways",
        ),
        (
            "fig_unaffected_service_interruption",
            "removal_ratio",
            "unaffected_interruption_ms",
            "Agent removal ratio",
            "Unaffected-flow interruption (ms)",
        ),
        (
            "fig_agent_removal_position",
            "removal_position",
            "elastic_latency_ms",
            "Removed Agent position",
            "Elastic latency (ms)",
        ),
    )
    for name, family, metric, x_label, y_label in definitions:
        data = _metric_rows(rows, family, metric)
        _write_plot_csv(output_dir / f"{name}.csv", data)
        outputs.extend(
            _line_chart(
                output_dir / name,
                data,
                title=name.replace("fig_", "").replace("_", " ").title(),
                x_label=x_label,
                y_label=y_label,
            )
        )

    breakdown = _breakdown_rows(rows)
    breakdown_name = "fig_elastic_latency_breakdown"
    _write_plot_csv(output_dir / f"{breakdown_name}.csv", breakdown)
    outputs.extend(
        _stacked_breakdown_chart(
            output_dir / breakdown_name,
            breakdown,
            title="Elastic Latency Breakdown",
        )
    )

    success_data = _success_residual_rows(rows)
    success_name = "fig_success_and_residual_rule_rate"
    _write_plot_csv(output_dir / f"{success_name}.csv", success_data)
    outputs.extend(
        _success_residual_chart(
            output_dir / success_name,
            success_data,
            title="Success and Residual-Rule Rate",
        )
    )
    return outputs


def _metric_rows(
    rows: list[dict[str, str]],
    family: str,
    metric: str,
) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        if row["scenario_family"] != family:
            continue
        mean = row.get(f"{metric}_mean", "")
        if mean == "":
            continue
        output.append(
            {
                "x": row["scenario_value"],
                "method": row["method"],
                "mean": float(mean),
                "ci95": float(row.get(f"{metric}_ci95", 0.0) or 0.0),
                "sample_count": int(float(row.get(f"{metric}_count", 0) or 0)),
                "success_rate": float(row["success_rate"]),
            }
        )
    return output


def _breakdown_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    preferred = [row for row in rows if row["scenario"] == "agent_count:20"]
    source = preferred or [row for row in rows if row["scenario_family"] == "agent_count"]
    components = (
        "scope_latency_ms",
        "compile_delta_latency_ms",
        "stage_latency_ms",
        "verification_latency_ms",
        "activation_latency_ms",
    )
    output = []
    for row in source:
        for component in components:
            value = row.get(f"{component}_mean", "")
            if value == "":
                continue
            output.append(
                {
                    "method": row["method"],
                    "component": component.removesuffix("_latency_ms"),
                    "mean_ms": float(value),
                }
            )
    return output


def _success_residual_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "x": row["scenario_value"],
            "method": row["method"],
            "success_rate": float(row["success_rate"]),
            "residual_rule_rate": float(row["residual_rule_rate"]),
        }
        for row in rows
        if row["scenario_family"] == "removal_ratio"
    ]


def _line_chart(
    base_path: Path,
    rows: list[dict[str, Any]],
    *,
    title: str,
    x_label: str,
    y_label: str,
) -> list[Path]:
    image, draw, font, small = _canvas(title)
    plot = (120, 90, 1340, 790)
    _axes(draw, plot, x_label, y_label, font, small)
    x_values = _ordered_values([str(row["x"]) for row in rows])
    maximum = max(
        (float(row["mean"]) + float(row.get("ci95", 0.0)) for row in rows),
        default=1.0,
    )
    maximum = max(maximum * 1.12, 1e-9)
    _y_ticks(draw, plot, maximum, small)
    x_positions = _x_positions(plot, x_values)
    for value, position in zip(x_values, x_positions):
        draw.text((position - 18, plot[3] + 10), value, fill="black", font=small)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["method"])].append(row)
    for method, method_rows in sorted(grouped.items()):
        by_x = {str(row["x"]): row for row in method_rows}
        points = []
        color = COLORS.get(method, "#6B7280")
        for x_value, x in zip(x_values, x_positions):
            row = by_x.get(x_value)
            if row is None:
                continue
            y = _map_y(float(row["mean"]), plot, maximum)
            error = float(row.get("ci95", 0.0))
            y_low = _map_y(max(0.0, float(row["mean"]) - error), plot, maximum)
            y_high = _map_y(float(row["mean"]) + error, plot, maximum)
            draw.line((x, y_low, x, y_high), fill=color, width=2)
            draw.line((x - 6, y_low, x + 6, y_low), fill=color, width=2)
            draw.line((x - 6, y_high, x + 6, y_high), fill=color, width=2)
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color)
            points.append((x, y))
        if len(points) > 1:
            draw.line(points, fill=color, width=4)
    _legend(draw, grouped, font)
    return _save_chart(image, base_path)


def _stacked_breakdown_chart(
    base_path: Path,
    rows: list[dict[str, Any]],
    *,
    title: str,
) -> list[Path]:
    image, draw, font, small = _canvas(title)
    plot = (120, 90, 1340, 790)
    _axes(draw, plot, "Method", "Latency (ms)", font, small)
    components = [
        "scope",
        "compile_delta",
        "stage",
        "verification",
        "activation",
    ]
    colors = ["#60A5FA", "#A78BFA", "#F59E0B", "#EF4444", "#10B981"]
    methods = sorted({str(row["method"]) for row in rows})
    values = {
        (str(row["method"]), str(row["component"])): float(row["mean_ms"])
        for row in rows
    }
    totals = [sum(values.get((method, component), 0.0) for component in components) for method in methods]
    maximum = max(totals, default=1.0) * 1.12 or 1.0
    _y_ticks(draw, plot, maximum, small)
    positions = _x_positions(plot, methods)
    for method, x in zip(methods, positions):
        bottom = plot[3]
        for component, color in zip(components, colors):
            value = values.get((method, component), 0.0)
            height = int(value / maximum * (plot[3] - plot[1]))
            draw.rectangle((x - 55, bottom - height, x + 55, bottom), fill=color)
            bottom -= height
        draw.text((x - 50, plot[3] + 12), method, fill="black", font=small)
    for index, (component, color) in enumerate(zip(components, colors)):
        y = 105 + index * 30
        draw.rectangle((1110, y, 1130, y + 16), fill=color)
        draw.text((1140, y - 3), component, fill="black", font=small)
    return _save_chart(image, base_path)


def _success_residual_chart(
    base_path: Path,
    rows: list[dict[str, Any]],
    *,
    title: str,
) -> list[Path]:
    expanded = []
    for row in rows:
        expanded.append(
            {
                "x": row["x"],
                "method": f"{row['method']}:success",
                "mean": row["success_rate"],
                "ci95": 0.0,
            }
        )
        expanded.append(
            {
                "x": row["x"],
                "method": f"{row['method']}:residual",
                "mean": row["residual_rule_rate"],
                "ci95": 0.0,
            }
        )
    image, draw, font, small = _canvas(title)
    plot = (120, 90, 1340, 790)
    _axes(draw, plot, "Agent removal ratio", "Rate", font, small)
    _y_ticks(draw, plot, 1.0, small)
    x_values = _ordered_values([str(row["x"]) for row in expanded])
    x_positions = _x_positions(plot, x_values)
    for value, position in zip(x_values, x_positions):
        draw.text((position - 18, plot[3] + 10), value, fill="black", font=small)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in expanded:
        grouped[str(row["method"])].append(row)
    palette = ["#2563EB", "#93C5FD", "#DC2626", "#FCA5A5", "#16A34A", "#86EFAC"]
    for index, (series, items) in enumerate(sorted(grouped.items())):
        color = palette[index % len(palette)]
        by_x = {str(row["x"]): row for row in items}
        points = [
            (x, _map_y(float(by_x[value]["mean"]), plot, 1.0))
            for value, x in zip(x_values, x_positions)
            if value in by_x
        ]
        if len(points) > 1:
            draw.line(points, fill=color, width=3)
        for x, y in points:
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color)
        grouped[series][0]["_color"] = color
    _legend(draw, grouped, small, color_key="_color")
    return _save_chart(image, base_path)


def _canvas(title: str):
    image = Image.new("RGB", (1400, 900), "white")
    draw = ImageDraw.Draw(image)
    font = _font(26)
    small = _font(18)
    draw.text((70, 25), title, fill="#111827", font=_font(32))
    return image, draw, font, small


def _font(size: int):
    path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default()


def _axes(draw, plot, x_label, y_label, font, small):
    draw.line((plot[0], plot[1], plot[0], plot[3]), fill="#111827", width=3)
    draw.line((plot[0], plot[3], plot[2], plot[3]), fill="#111827", width=3)
    draw.text(((plot[0] + plot[2]) // 2 - 100, 845), x_label, fill="black", font=font)
    draw.text((plot[0] + 5, 62), y_label, fill="black", font=small)


def _y_ticks(draw, plot, maximum, font):
    for index in range(6):
        value = maximum * index / 5
        y = _map_y(value, plot, maximum)
        draw.line((plot[0] - 6, y, plot[2], y), fill="#E5E7EB", width=1)
        label = f"{value:.0f}" if maximum >= 100 else f"{value:.2g}"
        draw.text((35, y - 10), label, fill="#374151", font=font)


def _map_y(value, plot, maximum):
    return int(plot[3] - value / max(maximum, 1e-12) * (plot[3] - plot[1]))


def _x_positions(plot, values):
    if not values:
        return []
    if len(values) == 1:
        return [(plot[0] + plot[2]) // 2]
    return [
        int(plot[0] + 70 + index * (plot[2] - plot[0] - 140) / (len(values) - 1))
        for index in range(len(values))
    ]


def _ordered_values(values):
    unique = set(values)
    position_order = ["leaf", "intermediate", "fan_in", "fan_out"]
    if unique <= set(position_order):
        return [value for value in position_order if value in unique]
    try:
        return sorted(unique, key=float)
    except ValueError:
        return sorted(unique)


def _legend(draw, grouped, font, color_key=None):
    for index, (method, rows) in enumerate(sorted(grouped.items())):
        y = 105 + index * 30
        color = rows[0].get(color_key, COLORS.get(method, "#6B7280")) if rows else "#6B7280"
        draw.line((1080, y + 8, 1120, y + 8), fill=color, width=5)
        draw.text((1130, y - 3), method, fill="black", font=font)


def _save_chart(image: Image.Image, base_path: Path) -> list[Path]:
    png = base_path.with_suffix(".png")
    pdf = base_path.with_suffix(".pdf")
    image.save(png, format="PNG")
    image.save(pdf, format="PDF", resolution=150.0)
    return [png, pdf]


def _write_plot_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0]) if rows else ["x", "method", "mean", "ci95"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plot experiment 3 processed results")
    parser.add_argument("--input", default="results/exp3/processed/summary.csv")
    parser.add_argument("--output-dir", default="results/exp3/figures")
    return parser


def main() -> None:
    args = _parser().parse_args()
    outputs = plot_exp3(Path(args.input), Path(args.output_dir))
    print(f"Wrote {len(outputs)} PDF/PNG figures to {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
