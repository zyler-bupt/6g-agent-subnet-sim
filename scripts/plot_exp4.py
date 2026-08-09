from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Callable

try:  # package execution: python -m scripts.plot_exp4
    from scripts.plot_exp3 import (
        COLORS,
        _axes,
        _canvas,
        _line_chart,
        _map_y,
        _save_chart,
        _write_plot_csv,
        _y_ticks,
    )
except ModuleNotFoundError:  # direct execution: python scripts/plot_exp4.py
    from plot_exp3 import (  # type: ignore[no-redef]
        COLORS,
        _axes,
        _canvas,
        _line_chart,
        _map_y,
        _save_chart,
        _write_plot_csv,
        _y_ticks,
    )


COLORS.update(
    {
        "network_only": "#16A34A",
        "no_scope": "#9333EA",
        "no_verification_rollback": "#F59E0B",
        "success": "#2563EB",
        "safe_rejection": "#DC2626",
    }
)
MAIN_METHODS = {"proposed", "full_rebuild", "network_only"}


def plot_exp4(summary_csv: Path, timeline_csv: Path, output_dir: Path) -> list[Path]:
    rows = _read_csv(summary_csv)
    timeline = _read_csv(timeline_csv)
    if not rows:
        raise ValueError("experiment 4 summary is empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []

    definitions: tuple[tuple[str, Callable, str, str], ...] = (
        (
            "fig_recovery_latency_by_failure_type",
            lambda: _family_metric(rows, "recovery_latency_ms"),
            "Failure type",
            "Recovery latency (ms)",
        ),
        (
            "fig_recovery_latency_vs_failure_severity",
            lambda: _severity_metric(rows, "recovery_latency_ms"),
            "Remaining capacity factor / Agent level",
            "Recovery latency (ms)",
        ),
        (
            "fig_recovery_success_rate",
            lambda: _family_rate(rows, "success_rate"),
            "Failure type",
            "Recovery success rate",
        ),
        (
            "fig_service_interruption",
            lambda: _family_metric(rows, "service_interruption_ms"),
            "Failure type",
            "Service interruption (ms)",
        ),
        (
            "fig_rule_change_ratio",
            lambda: _family_metric(rows, "rule_change_ratio"),
            "Failure type",
            "Changed rules / stable rules",
        ),
        (
            "fig_affected_gateway_ratio",
            lambda: _family_metric(rows, "gateway_impact_ratio"),
            "Failure type",
            "Affected gateways / task gateways",
        ),
        (
            "fig_collateral_service_impact",
            lambda: _family_metric(rows, "collateral_interruption_ms"),
            "Failure type",
            "Unaffected-flow interruption (ms)",
        ),
        (
            "fig_network_only_failure_modes",
            lambda: _network_only_modes(rows),
            "Failure type",
            "Rate",
        ),
    )
    for name, builder, x_label, y_label in definitions:
        data = builder()
        _write_plot_csv(output_dir / f"{name}.csv", data)
        outputs.extend(
            _line_chart(
                output_dir / name,
                data,
                title=name.removeprefix("fig_").replace("_", " ").title(),
                x_label=x_label,
                y_label=y_label,
            )
        )

    breakdown = _breakdown(rows)
    breakdown_name = "fig_recovery_latency_breakdown"
    _write_plot_csv(output_dir / f"{breakdown_name}.csv", breakdown)
    outputs.extend(
        _line_chart(
            output_dir / breakdown_name,
            breakdown,
            title="Recovery Latency Breakdown",
            x_label="Recovery phase",
            y_label="Latency (ms)",
        )
    )

    timeline_data = _timeline_rows(timeline)
    timeline_name = "fig_failure_recovery_timeline"
    _write_plot_csv(output_dir / f"{timeline_name}.csv", timeline_data)
    outputs.extend(_timeline_chart(output_dir / timeline_name, timeline_data))
    return outputs


def _family_metric(rows: list[dict[str, str]], metric: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[tuple[float, float, int]]] = defaultdict(list)
    for row in rows:
        if row["method"] not in MAIN_METHODS:
            continue
        value = row.get(f"{metric}_mean", "")
        if value == "":
            continue
        grouped[(row["fault_family"], row["method"])].append(
            (
                float(value),
                float(row.get(f"{metric}_ci95", 0.0) or 0.0),
                int(float(row.get(f"{metric}_count", 0) or 0)),
            )
        )
    output = []
    for (family, method), values in sorted(grouped.items()):
        count = sum(item[2] for item in values)
        output.append(
            {
                "x": _family_label(family),
                "method": method,
                "mean": _weighted_mean([(item[0], item[2]) for item in values]),
                "ci95": _weighted_mean([(item[1], item[2]) for item in values]),
                "sample_count": count,
            }
        )
    return output


def _family_rate(rows: list[dict[str, str]], metric: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    for row in rows:
        if row["method"] not in MAIN_METHODS:
            continue
        grouped[(row["fault_family"], row["method"])].append(
            (float(row[metric]), int(row["runs"]))
        )
    return [
        {
            "x": _family_label(family),
            "method": method,
            "mean": _weighted_mean(values),
            "ci95": 0.0,
            "sample_count": sum(count for _value, count in values),
        }
        for (family, method), values in sorted(grouped.items())
    ]


def _severity_metric(rows: list[dict[str, str]], metric: str) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        if row["method"] not in MAIN_METHODS:
            continue
        value = row.get(f"{metric}_mean", "")
        if value == "":
            continue
        family = _family_label(row["fault_family"])
        level = row["fault_level"].replace("capacity_factor_", "")
        output.append(
            {
                "x": level,
                "method": f"{row['method']}:{family}",
                "mean": float(value),
                "ci95": float(row.get(f"{metric}_ci95", 0.0) or 0.0),
                "sample_count": int(float(row.get(f"{metric}_count", 0) or 0)),
            }
        )
    return output


def _breakdown(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    components = (
        "detection_latency_ms",
        "localization_latency_ms",
        "proposal_latency_ms",
        "coordination_latency_ms",
        "stage_latency_ms",
        "verification_latency_ms",
        "activation_latency_ms",
    )
    output = []
    for method in sorted({row["method"] for row in rows} & MAIN_METHODS):
        source = [row for row in rows if row["method"] == method]
        for metric in components:
            values = [
                float(row[f"{metric}_mean"])
                for row in source
                if row.get(f"{metric}_mean", "") != ""
            ]
            output.append(
                {
                    "x": metric.removesuffix("_latency_ms"),
                    "method": method,
                    "mean": fmean(values) if values else 0.0,
                    "ci95": 0.0,
                    "sample_count": sum(int(row["runs"]) for row in source),
                }
            )
    return output


def _network_only_modes(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    source = [row for row in rows if row["method"] == "network_only"]
    output = []
    for row in source:
        output.extend(
            (
                {
                    "x": _family_label(row["fault_family"]),
                    "method": "success",
                    "mean": float(row["success_rate"]),
                    "ci95": 0.0,
                    "sample_count": int(row["runs"]),
                },
                {
                    "x": _family_label(row["fault_family"]),
                    "method": "safe_rejection",
                    "mean": float(row["safe_rejection_rate"]),
                    "ci95": 0.0,
                    "sample_count": int(row["runs"]),
                },
            )
        )
    return _collapse_plot_rows(output)


def _timeline_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    source = [row for row in rows if int(row["seed"]) == 0]
    output = []
    proposed = [row for row in source if row["method"] == "proposed"]
    for row in proposed:
        for metric, series in (
            ("application_demand_mbps", "Application demand"),
            ("network_capacity_mbps", "Network capacity"),
            ("physical_capacity_mbps", "Physical capacity"),
        ):
            output.append(
                {
                    "x": row["logical_time_s"],
                    "method": series,
                    "mean": float(row[metric]),
                    "ci95": 0.0,
                    "sample_count": 1,
                    "phase": row["phase"],
                    "qos_satisfied": row["qos_satisfied"],
                    "failure_detected": row["failure_detected"],
                    "activated": row["activated"],
                }
            )
    labels = {
        "proposed": "Throughput: Proposed",
        "full_rebuild": "Throughput: Full-Rebuild",
        "network_only": "Throughput: Network-Only",
    }
    for row in source:
        output.append(
            {
                "x": row["logical_time_s"],
                "method": labels[row["method"]],
                "mean": float(row["actual_throughput_mbps"]),
                "ci95": 0.0,
                "sample_count": 1,
                "phase": row["phase"],
                "qos_satisfied": row["qos_satisfied"],
                "failure_detected": row["failure_detected"],
                "activated": row["activated"],
            }
        )
    return output


def _timeline_chart(base_path: Path, rows: list[dict[str, Any]]) -> list[Path]:
    image, draw, font, small = _canvas("Compound Failure Recovery Timeline")
    # Reserve a dedicated header band for the six series.  The timeline uses a
    # true numeric x-axis so the closely spaced decision/activation events do
    # not receive the same visual width as the two-second scenario intervals.
    plot = (120, 245, 1340, 790)
    _axes(draw, plot, "Logical time (s)", "Rate / capacity (Mbps)", font, small)
    x_values = sorted({float(row["x"]) for row in rows})
    maximum = max((float(row["mean"]) for row in rows), default=1.0) * 1.12
    _y_ticks(draw, plot, maximum, small)
    x_min = min(x_values, default=0.0)
    x_max = max(x_values, default=1.0)
    x_span = max(x_max - x_min, 1.0)
    x_positions = {
        value: plot[0] + (value - x_min) * (plot[2] - plot[0]) / x_span
        for value in x_values
    }
    labelled_ticks = [
        value
        for value in x_values
        if value in {x_values[0], x_values[-1]} or abs(value - round(value / 2.0) * 2.0) < 1e-9
    ]
    for value in labelled_ticks:
        x = x_positions[value]
        draw.text((x - 14, plot[3] + 10), f"{value:g}", fill="black", font=small)

    series_colors = {
        "Application demand": "#111827",
        "Network capacity": "#7C3AED",
        "Physical capacity": "#F59E0B",
        "Throughput: Proposed": "#2563EB",
        "Throughput: Full-Rebuild": "#DC2626",
        "Throughput: Network-Only": "#16A34A",
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["method"])].append(row)
    for name, values in grouped.items():
        values.sort(key=lambda row: float(row["x"]))
        color = series_colors.get(name, "#6B7280")
        points = []
        for row in values:
            x = x_positions[float(row["x"])]
            y = _map_y(float(row["mean"]), plot, maximum)
            points.append((x, y))
            if name.startswith("Throughput"):
                qos = str(row.get("qos_satisfied", "")).lower() == "true"
                outline = "#16A34A" if qos else "#DC2626"
                draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=color, outline=outline, width=3)
            else:
                draw.rectangle((x - 4, y - 4, x + 4, y + 4), fill=color)
        if len(points) > 1:
            draw.line(points, fill=color, width=4)

    fault_times = sorted(
        {
            float(row["x"])
            for row in rows
            if str(row.get("failure_detected", "")).lower() == "true"
        }
    )
    activation_times = sorted(
        {
            float(row["x"])
            for row in rows
            if str(row.get("activated", "")).lower() == "true"
        }
    )
    if fault_times:
        x = x_positions[fault_times[0]]
        draw.line((x, plot[1], x, plot[3]), fill="#DC2626", width=2)
        draw.text((x + 5, plot[1] + 4), "Fault detected", fill="#DC2626", font=small)
    if activation_times:
        x = x_positions[activation_times[0]]
        draw.line((x, plot[1], x, plot[3]), fill="#16A34A", width=2)
        draw.text((x + 5, plot[1] + 28), "Activation", fill="#16A34A", font=small)

    for index, (name, color) in enumerate(series_colors.items()):
        column = index % 3
        row_index = index // 3
        x = 120 + column * 410
        y = 105 + row_index * 42
        draw.line((x, y + 8, x + 38, y + 8), fill=color, width=4)
        draw.text((x + 46, y), name, fill="black", font=small)
    draw.text((120, 195), "Throughput marker outline: green=QoS met, red=QoS violated", fill="#374151", font=small)
    return _save_chart(image, base_path)


def _collapse_plot_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["x"]), str(row["method"]))].append(row)
    return [
        {
            "x": key[0],
            "method": key[1],
            "mean": fmean(float(row["mean"]) for row in values),
            "ci95": 0.0,
            "sample_count": sum(int(row["sample_count"]) for row in values),
        }
        for key, values in sorted(grouped.items())
    ]


def _weighted_mean(values: list[tuple[float, int]]) -> float:
    total = sum(count for _value, count in values)
    return sum(value * count for value, count in values) / total if total else 0.0


def _family_label(value: str) -> str:
    return {
        "AGENT_FAILURE": "Agent",
        "LINK_FAILURE": "Link",
        "PHYSICAL_CAPACITY_DROP": "Physical",
    }.get(value, value)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plot experiment 4 recovery results")
    parser.add_argument("--summary", default="results/exp4/processed/summary.csv")
    parser.add_argument("--timeline", default="results/exp4/raw/timeline.csv")
    parser.add_argument("--output-dir", default="results/exp4/figures")
    return parser


def main() -> None:
    args = _parser().parse_args()
    outputs = plot_exp4(Path(args.summary), Path(args.timeline), Path(args.output_dir))
    print(f"Wrote {len(outputs)} Exp4 figure files to {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
