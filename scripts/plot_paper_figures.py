from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, stdev
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont


FONT_REGULAR = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
BACKGROUND = "#FFFFFF"
INK = "#111827"
GRID = "#D1D5DB"
SUBTLE = "#6B7280"


@dataclass(frozen=True)
class MethodStyle:
    label: str
    color: str
    line_pattern: tuple[int, ...]
    marker: str
    hatch: str


METHOD_STYLES = {
    "proposed": MethodStyle("Proposed", "#172033", (), "circle", "diag"),
    "full_rebuild": MethodStyle("Full-Rebuild", "#6B7280", (18, 9), "square", "cross"),
    "adjacent": MethodStyle("Adjacent-Layer", "#4B5563", (18, 7, 4, 7), "triangle", "vertical"),
    "independent": MethodStyle("Independent-Layer", "#9CA3AF", (4, 7), "diamond", "horizontal"),
    "no_verification": MethodStyle("w/o Verification", "#374151", (25, 8, 5, 8), "x", "dots"),
    "network_only": MethodStyle("Network-Only", "#9CA3AF", (), "diamond", "diag_back"),
    "t_form": MethodStyle("Txn-verified T_form", "#172033", (), "circle", "diag"),
    "t_ctrl": MethodStyle("Controller T_ctrl", "#6B7280", (18, 9), "square", "cross"),
}


COMPONENT_STYLES = {
    "Mapping": ("#E5E7EB", "horizontal"),
    "Cross-Layer Coordination": ("#CBD5E1", "diag"),
    "Compilation": ("#B6C1CC", "vertical"),
    "Installation": ("#94A3B8", "diag_back"),
    "Activation": ("#64748B", "cross"),
    "Verification": ("#475569", "dots"),
    "Localization": ("#E5E7EB", "horizontal"),
    "Proposal": ("#D1D5DB", "vertical"),
    "Coordination": ("#BFC8D3", "diag"),
    "Delta Compilation": ("#AEB9C6", "diag_back"),
    "Stage": ("#94A3B8", "cross"),
}


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(str(path), size=size)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError(f"refusing to write empty plotting data: {path}")
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _mean_ci(values: Iterable[float]) -> tuple[float, float, int]:
    materialized = list(values)
    if not materialized:
        return 0.0, 0.0, 0
    deviation = stdev(materialized) if len(materialized) > 1 else 0.0
    return (
        fmean(materialized),
        1.96 * deviation / math.sqrt(len(materialized)) if len(materialized) > 1 else 0.0,
        len(materialized),
    )


def _group_stats(
    rows: Iterable[dict[str, str]],
    keys: tuple[str, ...],
    value,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[float]] = defaultdict(list)
    for row in rows:
        groups[tuple(row[key] for key in keys)].append(float(value(row)))
    output = []
    for key, values in sorted(groups.items()):
        mean, ci95, count = _mean_ci(values)
        output.append(
            {
                **dict(zip(keys, key)),
                "mean": mean,
                "ci95": ci95,
                "sample_count": count,
            }
        )
    return output


def build_fig1_data(exp1_raw: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = [row for row in _read_csv(exp1_raw) if _as_bool(row["success"])]
    panel_a: list[dict[str, Any]] = []
    for num_agents in sorted({int(row["num_agents"]) for row in rows}):
        source = [row for row in rows if int(row["num_agents"]) == num_agents]
        verification_mean = fmean(float(row["verification_latency_ms"]) for row in source)
        formation_mean = fmean(float(row["formation_latency_ms"]) for row in source)
        for series, field in (
            ("t_form", "formation_latency_ms"),
            ("t_ctrl", "control_plane_latency_ms"),
        ):
            mean, ci95, count = _mean_ci(float(row[field]) for row in source)
            panel_a.append(
                {
                    "num_agents": num_agents,
                    "series": series,
                    "mean_ms": mean,
                    "ci95_ms": ci95,
                    "sample_count": count,
                    "verification_scope": "in_memory_transactional_business_edge_verifier",
                    "contains_ping": False,
                    "contains_iperf3": False,
                    "verification_mean_ms": verification_mean,
                    "verification_share_percent": (
                        100.0 * verification_mean / formation_mean if formation_mean else 0.0
                    ),
                }
            )
    size_map = {10: "Small", 40: "Medium", 80: "Large"}
    component_fields = (
        ("Mapping", "mapping_latency_ms"),
        ("Cross-Layer Coordination", "cross_layer_coordination_latency_ms"),
        ("Compilation", "compilation_and_planning_latency_ms"),
        ("Installation", "installation_latency_ms"),
        ("Activation", "activation_latency_ms"),
        ("Verification", "verification_latency_ms"),
    )
    panel_b: list[dict[str, Any]] = []
    for agents, label in size_map.items():
        source = [row for row in rows if int(row["num_agents"]) == agents]
        for component, field in component_fields:
            mean, ci95, count = _mean_ci(float(row[field]) for row in source)
            panel_b.append(
                {
                    "task_size": label,
                    "num_agents": agents,
                    "component": component,
                    "mean_ms": mean,
                    "ci95_ms": ci95,
                    "sample_count": count,
                }
            )
    return panel_a, panel_b


def build_fig2_data(
    exp2_raw: Path,
    robustness_raw: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _read_csv(exp2_raw)
    methods = {"proposed", "adjacent", "independent", "no_verification"}

    def conflict_panel(prefix: str, x_transform) -> list[dict[str, Any]]:
        source = [
            row for row in rows
            if row["scenario"].startswith(prefix + ":") and row["method"] in methods
        ]
        output = _group_stats(
            source,
            ("conflict_pressure", "method"),
            lambda row: 100.0 if _as_bool(row["qos_satisfied"]) else 0.0,
        )
        for row in output:
            row["x"] = x_transform(float(row.pop("conflict_pressure")))
            row["mean_percent"] = row.pop("mean")
            row["ci95_percent"] = row.pop("ci95")
        return sorted(output, key=lambda row: (float(row["x"]), row["method"]))

    application = conflict_panel("application_capacity", lambda value: value)
    transport = conflict_panel("transport_network", lambda value: value * 100.0)
    physical = conflict_panel("network_physical", lambda value: value * 100.0)

    robust = _read_csv(robustness_raw)
    noise_source = [
        row for row in robust
        if row["experiment"] == "noise" and row["method"] in {"proposed", "adjacent"}
    ]
    noise = _group_stats(
        noise_source,
        ("noise_ratio", "method"),
        lambda row: 100.0 if _as_bool(row["qos_satisfied"]) else 0.0,
    )
    for row in noise:
        row["x"] = float(row.pop("noise_ratio")) * 100.0
        row["mean_percent"] = row.pop("mean")
        row["ci95_percent"] = row.pop("ci95")
        row["ground_truth_state"] = "true_unnoised_state"
    noise.sort(key=lambda row: (float(row["x"]), row["method"]))

    scale_source = [
        row for row in robust
        if row["experiment"] == "proposal_scale" and row["method"] in {"proposed", "adjacent"}
    ]
    scale = _group_stats(
        scale_source,
        ("proposals_per_layer", "method"),
        lambda row: float(row["coordination_latency_ms"]),
    )
    for row in scale:
        proposals = int(row.pop("proposals_per_layer"))
        row["x"] = proposals
        row["mean_ms"] = row.pop("mean")
        row["ci95_ms"] = row.pop("ci95")
        row["num_raw_combinations"] = proposals**4
        row["solver"] = (
            "Proposed Exact Search" if row["method"] == "proposed" else "Adjacent-Layer"
        )
    scale.sort(key=lambda row: (int(row["x"]), row["method"]))
    return application, transport, physical, noise, scale


def build_fig3_data(
    exp3_raw: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _read_csv(exp3_raw)
    latency_source = [
        row for row in rows
        if row["scenario"].startswith("agent_count:")
        and row["method"] in {"proposed", "full_rebuild"}
        and _as_bool(row["success"])
    ]
    latency = _group_stats(
        latency_source,
        ("num_agents_before", "method"),
        lambda row: float(row["elastic_latency_ms"]),
    )
    for row in latency:
        row["x"] = int(row.pop("num_agents_before"))
        row["mean_ms"] = row.pop("mean")
        row["ci95_ms"] = row.pop("ci95")
    latency.sort(key=lambda row: (int(row["x"]), row["method"]))

    ratio_source = [
        row for row in rows
        if row["scenario"].startswith("removal_ratio:")
        and row["method"] in {"proposed", "full_rebuild"}
    ]
    changed = _group_stats(
        ratio_source,
        ("agent_removal_ratio", "method"),
        lambda row: float(row["changed_rules_ratio"]),
    )
    for row in changed:
        row["x_percent"] = float(row.pop("agent_removal_ratio")) * 100.0
        row["mean_ratio"] = row.pop("mean")
        row["ci95_ratio"] = row.pop("ci95")
    changed.sort(key=lambda row: (float(row["x_percent"]), row["method"]))

    local_source = [
        row for row in rows
        if row["scenario"].startswith("removal_ratio:") and row["method"] == "local_only"
    ]
    local: list[dict[str, Any]] = []
    for ratio in sorted({float(row["agent_removal_ratio"]) for row in local_source}):
        source = [row for row in local_source if float(row["agent_removal_ratio"]) == ratio]
        success = sum(_as_bool(row["success"]) for row in source)
        local.extend(
            (
                {
                    "removed_agents_percent": ratio * 100.0,
                    "outcome": "Success",
                    "percent": 100.0 * success / len(source),
                    "runs": len(source),
                },
                {
                    "removed_agents_percent": ratio * 100.0,
                    "outcome": "Verification Failure / Rollback",
                    "percent": 100.0 * (len(source) - success) / len(source),
                    "runs": len(source),
                },
            )
        )
    return latency, changed, local


def _fault_family(row: dict[str, str]) -> str:
    if row["fault_type"] == "AGENT_FAILURE":
        return "Agent Failure"
    if row["fault_type"] in {"LINK_FAILURE", "LINK_DEGRADATION"}:
        return "Link Failure"
    return "Physical Capacity Drop"


def build_fig4_data(
    exp4_raw: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _read_csv(exp4_raw)
    main_methods = {"proposed", "full_rebuild", "network_only"}
    main = [row for row in rows if row["method"] in main_methods]
    for row in main:
        row["paper_fault_family"] = _fault_family(row)
    success = _group_stats(
        main,
        ("paper_fault_family", "method"),
        lambda row: 100.0 if _as_bool(row["recovery_success"]) else 0.0,
    )
    for row in success:
        row["mean_percent"] = row.pop("mean")
        row["ci95_percent"] = row.pop("ci95")

    rule_source = [row for row in main if row["method"] in {"proposed", "full_rebuild"}]
    rules = _group_stats(
        rule_source,
        ("paper_fault_family", "method"),
        lambda row: float(row["rule_change_ratio"]),
    )
    by_family = defaultdict(dict)
    for row in rules:
        row["mean_ratio"] = row.pop("mean")
        row["ci95_ratio"] = row.pop("ci95")
        by_family[row["paper_fault_family"]][row["method"]] = row["mean_ratio"]
    for row in rules:
        values = by_family[row["paper_fault_family"]]
        if row["method"] == "proposed" and values.get("full_rebuild", 0.0) > 0.0:
            row["reduction_percent"] = 100.0 * (
                1.0 - values["proposed"] / values["full_rebuild"]
            )
        else:
            row["reduction_percent"] = ""

    recovery_source = [
        row for row in main
        if row["method"] in {"proposed", "full_rebuild"} and _as_bool(row["success"])
    ]
    components = (
        "Localization",
        "Proposal",
        "Coordination",
        "Delta Compilation",
        "Stage",
        "Verification",
        "Activation",
    )
    breakdown_values: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in recovery_source:
        known = {
            "Localization": float(row["localization_latency_ms"]),
            "Proposal": float(row["proposal_latency_ms"]),
            "Coordination": float(row["coordination_latency_ms"])
            + float(row["feasibility_latency_ms"]),
            "Delta Compilation": float(row["delta_compile_latency_ms"]),
            "Stage": float(row["stage_latency_ms"]),
            "Activation": float(row["activation_latency_ms"]),
        }
        verification = max(
            0.0,
            float(row["repair_latency_ms"]) - sum(known.values()),
        )
        known["Verification"] = verification
        family = row["paper_fault_family"]
        for component in components:
            breakdown_values[(family, row["method"], component)].append(known[component])
    breakdown: list[dict[str, Any]] = []
    for (family, method, component), values in sorted(breakdown_values.items()):
        mean, ci95, count = _mean_ci(values)
        breakdown.append(
            {
                "paper_fault_family": family,
                "method": method,
                "component": component,
                "mean_ms": mean,
                "ci95_ms": ci95,
                "sample_count": count,
                "detection_interval_excluded": True,
                "verification_includes_stable_health_windows": component == "Verification",
            }
        )
    family_order = {"Agent Failure": 0, "Link Failure": 1, "Physical Capacity Drop": 2}
    success.sort(key=lambda row: (family_order[row["paper_fault_family"]], row["method"]))
    rules.sort(key=lambda row: (family_order[row["paper_fault_family"]], row["method"]))
    breakdown.sort(
        key=lambda row: (
            family_order[row["paper_fault_family"]],
            row["method"],
            components.index(row["component"]),
        )
    )
    return success, rules, breakdown


def _draw_pattern_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    finish: tuple[float, float],
    color: str,
    width: int,
    pattern: tuple[int, ...],
) -> None:
    if not pattern:
        draw.line((start, finish), fill=color, width=width)
        return
    dx, dy = finish[0] - start[0], finish[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 0:
        return
    position = 0.0
    index = 0
    while position < length:
        segment = pattern[index % len(pattern)]
        end = min(length, position + segment)
        if index % 2 == 0:
            p1 = (start[0] + dx * position / length, start[1] + dy * position / length)
            p2 = (start[0] + dx * end / length, start[1] + dy * end / length)
            draw.line((p1, p2), fill=color, width=width)
        position = end
        index += 1


def _marker(draw: ImageDraw.ImageDraw, point: tuple[float, float], style: MethodStyle, size: int = 8) -> None:
    x, y = point
    if style.marker == "circle":
        draw.ellipse((x - size, y - size, x + size, y + size), fill=BACKGROUND, outline=style.color, width=4)
    elif style.marker == "square":
        draw.rectangle((x - size, y - size, x + size, y + size), fill=BACKGROUND, outline=style.color, width=4)
    elif style.marker == "triangle":
        draw.polygon(((x, y - size), (x - size, y + size), (x + size, y + size)), fill=BACKGROUND, outline=style.color)
        draw.line(((x, y - size), (x - size, y + size), (x + size, y + size), (x, y - size)), fill=style.color, width=4)
    elif style.marker == "diamond":
        draw.polygon(((x, y - size), (x - size, y), (x, y + size), (x + size, y)), fill=BACKGROUND, outline=style.color)
        draw.line(((x, y - size), (x - size, y), (x, y + size), (x + size, y), (x, y - size)), fill=style.color, width=4)
    else:
        draw.line((x - size, y - size, x + size, y + size), fill=style.color, width=4)
        draw.line((x - size, y + size, x + size, y - size), fill=style.color, width=4)


def _text_center(draw: ImageDraw.ImageDraw, text: str, center_x: float, y: float, font, fill=INK) -> None:
    bounds = draw.textbbox((0, 0), text, font=font)
    draw.text((center_x - (bounds[2] - bounds[0]) / 2, y), text, fill=fill, font=font)


def _axes(
    draw: ImageDraw.ImageDraw,
    plot: tuple[int, int, int, int],
    *,
    y_max: float,
    y_label: str,
    x_label: str,
    percent: bool = False,
) -> None:
    left, top, right, bottom = plot
    small = _font(24)
    for index in range(6):
        value = y_max * index / 5.0
        y = bottom - (bottom - top) * index / 5.0
        draw.line((left, y, right, y), fill=GRID, width=2)
        label = f"{value:.0f}" if percent or y_max >= 10 else f"{value:.2f}"
        bounds = draw.textbbox((0, 0), label, font=small)
        draw.text((left - 14 - (bounds[2] - bounds[0]), y - 14), label, fill=INK, font=small)
    draw.line((left, top, left, bottom), fill=INK, width=3)
    draw.line((left, bottom, right, bottom), fill=INK, width=3)
    _text_center(draw, x_label, (left + right) / 2, bottom + 56, _font(27))
    label_font = _font(23)
    bounds = label_font.getbbox(y_label)
    label = Image.new(
        "RGBA",
        (bounds[2] - bounds[0] + 12, bounds[3] - bounds[1] + 12),
        (255, 255, 255, 0),
    )
    label_draw = ImageDraw.Draw(label)
    label_draw.text((6 - bounds[0], 6 - bounds[1]), y_label, fill=INK, font=label_font)
    rotated = label.rotate(90, expand=True)
    image = draw._image  # Pillow's ImageDraw keeps the target image here.
    image.paste(
        rotated,
        (
            int(left - 82 - rotated.width / 2),
            int((top + bottom) / 2 - rotated.height / 2),
        ),
        rotated,
    )


def _panel_header(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    title: str,
) -> None:
    left, top, right, _bottom = box
    draw.text((left + 18, top + 16), label, fill=INK, font=_font(31, bold=True))
    title_size = 29
    title_font = _font(title_size, bold=True)
    maximum_width = right - left - 165
    while title_size > 21:
        bounds = draw.textbbox((0, 0), title, font=title_font)
        if bounds[2] - bounds[0] <= maximum_width:
            break
        title_size -= 1
        title_font = _font(title_size, bold=True)
    _text_center(draw, title, (left + right) / 2 + 20, top + 18, title_font)


def _legend(
    draw: ImageDraw.ImageDraw,
    entries: list[tuple[str, str]],
    box: tuple[int, int, int, int],
    *,
    columns: int,
) -> int:
    left, top, right, _bottom = box
    column_width = (right - left) / columns
    row_height = 38
    for index, (key, label) in enumerate(entries):
        row = index // columns
        column = index % columns
        x = left + column * column_width
        y = top + row * row_height
        style = METHOD_STYLES[key]
        _draw_pattern_line(draw, (x, y + 11), (x + 48, y + 11), style.color, 4, style.line_pattern)
        _marker(draw, (x + 24, y + 11), style, size=6)
        draw.text((x + 60, y - 2), label, fill=INK, font=_font(22))
    return math.ceil(len(entries) / columns) * row_height


def _line_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    rows: list[dict[str, Any]],
    *,
    panel_label: str,
    title: str,
    x_key: str,
    series_key: str,
    mean_key: str,
    ci_key: str,
    x_label: str,
    y_label: str,
    series_order: list[str],
    x_formatter=lambda value: f"{value:g}",
    y_max: float | None = None,
    percent: bool = False,
    scope_note: str = "",
) -> None:
    _panel_header(draw, box, panel_label, title)
    left, top, right, bottom = box
    entries = [(key, METHOD_STYLES[key].label) for key in series_order]
    legend_top = top + 70
    legend_height = _legend(draw, entries, (left + 100, legend_top, right - 20, bottom), columns=min(2, len(entries)))
    plot_top = legend_top + legend_height + (32 if scope_note else 8)
    if scope_note:
        _text_center(draw, scope_note, (left + right) / 2, legend_top + legend_height, _font(19), fill=SUBTLE)
    plot = (left + 100, plot_top, right - 35, bottom - 100)
    maximum = y_max or max(float(row[mean_key]) + float(row.get(ci_key, 0.0)) for row in rows) * 1.12
    maximum = max(maximum, 1e-9)
    _axes(draw, plot, y_max=maximum, y_label=y_label, x_label=x_label, percent=percent)
    x_values = sorted({float(row[x_key]) for row in rows})
    span = max(len(x_values) - 1, 1)
    positions = {
        value: plot[0] + index * (plot[2] - plot[0]) / span
        for index, value in enumerate(x_values)
    }
    for value, x in positions.items():
        _text_center(draw, x_formatter(value), x, plot[3] + 14, _font(22))
    for series in series_order:
        source = sorted(
            (row for row in rows if row[series_key] == series),
            key=lambda row: float(row[x_key]),
        )
        style = METHOD_STYLES[series]
        points: list[tuple[float, float]] = []
        for row in source:
            x = positions[float(row[x_key])]
            mean = float(row[mean_key])
            ci = float(row.get(ci_key, 0.0))
            y = plot[3] - mean / maximum * (plot[3] - plot[1])
            low = plot[3] - max(0.0, mean - ci) / maximum * (plot[3] - plot[1])
            high = plot[3] - min(maximum, mean + ci) / maximum * (plot[3] - plot[1])
            draw.line((x, low, x, high), fill="#9CA3AF", width=2)
            draw.line((x - 6, low, x + 6, low), fill="#9CA3AF", width=2)
            draw.line((x - 6, high, x + 6, high), fill="#9CA3AF", width=2)
            points.append((x, y))
        for start, finish in zip(points, points[1:]):
            _draw_pattern_line(draw, start, finish, style.color, 4, style.line_pattern)
        for point in points:
            _marker(draw, point, style)


def _hatch(draw: ImageDraw.ImageDraw, rect: tuple[float, float, float, float], style: str, color="#4B5563") -> None:
    left, top, right, bottom = (int(value) for value in rect)
    width = max(1, right - left)
    height = max(1, bottom - top)
    layer = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    hatch_draw = ImageDraw.Draw(layer)
    spacing = 13
    if style in {"diag", "cross"}:
        for offset in range(-height, width + height, spacing):
            hatch_draw.line((offset, height, offset + height, 0), fill=color, width=2)
    if style in {"diag_back", "cross"}:
        for offset in range(-height, width + height, spacing):
            hatch_draw.line((offset, 0, offset + height, height), fill=color, width=2)
    if style == "horizontal":
        for y in range(spacing, height, spacing):
            hatch_draw.line((0, y, width, y), fill=color, width=2)
    if style == "vertical":
        for x in range(spacing, width, spacing):
            hatch_draw.line((x, 0, x, height), fill=color, width=2)
    if style == "dots":
        for y in range(7, height, spacing):
            for x in range(7, width, spacing):
                hatch_draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=color)
    draw._image.paste(layer, (left, top), layer)


def _bar(draw, rect, fill, hatch, outline=INK) -> None:
    draw.rectangle(rect, fill=fill)
    _hatch(draw, rect, hatch)
    draw.rectangle(rect, outline=outline, width=3)


def _stacked_breakdown_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    rows: list[dict[str, Any]],
    *,
    panel_label: str,
    title: str,
    group_key: str,
    component_order: list[str],
    task_groups: list[str],
    y_label: str,
    x_label: str,
    method_pairs: bool = False,
) -> None:
    _panel_header(draw, box, panel_label, title)
    left, top, right, bottom = box
    legend_columns = 3 if len(component_order) <= 6 else 4
    legend_top = top + 72
    legend_width = (right - left - 110) / legend_columns
    for index, component in enumerate(component_order):
        column = index % legend_columns
        row_index = index // legend_columns
        x = left + 95 + column * legend_width
        y = legend_top + row_index * 35
        color, hatch = COMPONENT_STYLES[component]
        _bar(draw, (x, y, x + 28, y + 22), color, hatch)
        draw.text((x + 38, y - 3), component, fill=INK, font=_font(18))
    plot_top = legend_top + math.ceil(len(component_order) / legend_columns) * 35 + 14
    plot = (left + 100, plot_top, right - 35, bottom - 110)
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for row in rows:
        totals[(str(row[group_key]), str(row.get("method", "")))] += float(row["mean_ms"])
    maximum = max(totals.values(), default=1.0) * 1.13
    _axes(draw, plot, y_max=maximum, y_label=y_label, x_label=x_label)
    if method_pairs:
        bar_width = min(58, (plot[2] - plot[0]) / (len(task_groups) * 3.0))
        family_centers = []
        for group_index, group in enumerate(task_groups):
            center = plot[0] + (group_index + 0.5) * (plot[2] - plot[0]) / len(task_groups)
            family_centers.append(center)
            for method_index, method in enumerate(("proposed", "full_rebuild")):
                x_center = center + (-0.62 if method_index == 0 else 0.62) * bar_width
                y_cursor = plot[3]
                for component in component_order:
                    match = next(
                        row for row in rows
                        if row[group_key] == group and row["method"] == method and row["component"] == component
                    )
                    height = float(match["mean_ms"]) / maximum * (plot[3] - plot[1])
                    rect = (x_center - bar_width / 2, y_cursor - height, x_center + bar_width / 2, y_cursor)
                    color, hatch = COMPONENT_STYLES[component]
                    _bar(draw, rect, color, hatch)
                    y_cursor -= height
                _text_center(draw, "P" if method == "proposed" else "F", x_center, plot[3] + 10, _font(21, bold=True))
            _text_center(draw, group.replace(" Failure", "\nFailure") if False else group, center, plot[3] + 39, _font(17))
    else:
        bar_width = min(100, (plot[2] - plot[0]) / (len(task_groups) * 2.0))
        for index, group in enumerate(task_groups):
            x_center = plot[0] + (index + 0.5) * (plot[2] - plot[0]) / len(task_groups)
            y_cursor = plot[3]
            for component in component_order:
                match = next(row for row in rows if row[group_key] == group and row["component"] == component)
                height = float(match["mean_ms"]) / maximum * (plot[3] - plot[1])
                rect = (x_center - bar_width / 2, y_cursor - height, x_center + bar_width / 2, y_cursor)
                color, hatch = COMPONENT_STYLES[component]
                _bar(draw, rect, color, hatch)
                y_cursor -= height
            _text_center(draw, group, x_center, plot[3] + 15, _font(21))


def _grouped_bar_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    rows: list[dict[str, Any]],
    *,
    panel_label: str,
    title: str,
    group_key: str,
    groups: list[str],
    series_order: list[str],
    mean_key: str,
    ci_key: str,
    y_label: str,
    x_label: str,
    y_max: float | None = None,
    percent: bool = False,
    annotate_reduction: bool = False,
) -> None:
    _panel_header(draw, box, panel_label, title)
    left, top, right, bottom = box
    _legend(
        draw,
        [(series, METHOD_STYLES[series].label) for series in series_order],
        (left + 100, top + 72, right - 20, bottom),
        columns=min(len(series_order), 3),
    )
    plot = (left + 100, top + 135, right - 35, bottom - 100)
    maximum = y_max or max(float(row[mean_key]) + float(row.get(ci_key, 0.0)) for row in rows) * 1.18
    _axes(draw, plot, y_max=maximum, y_label=y_label, x_label=x_label, percent=percent)
    group_width = (plot[2] - plot[0]) / len(groups)
    bar_width = min(62, group_width / (len(series_order) + 1.2))
    for group_index, group in enumerate(groups):
        center = plot[0] + (group_index + 0.5) * group_width
        for series_index, series in enumerate(series_order):
            row = next(item for item in rows if item[group_key] == group and item["method"] == series)
            x = center + (series_index - (len(series_order) - 1) / 2) * bar_width * 1.12
            mean = float(row[mean_key])
            ci = float(row.get(ci_key, 0.0))
            y = plot[3] - mean / maximum * (plot[3] - plot[1])
            style = METHOD_STYLES[series]
            _bar(draw, (x - bar_width / 2, y, x + bar_width / 2, plot[3]), "#E5E7EB", style.hatch)
            error_top = plot[3] - min(maximum, mean + ci) / maximum * (plot[3] - plot[1])
            error_bottom = plot[3] - max(0.0, mean - ci) / maximum * (plot[3] - plot[1])
            draw.line((x, error_top, x, error_bottom), fill="#6B7280", width=2)
            draw.line((x - 6, error_top, x + 6, error_top), fill="#6B7280", width=2)
            if annotate_reduction and series == "proposed" and row.get("reduction_percent", "") != "":
                _text_center(draw, f"↓{float(row['reduction_percent']):.1f}%", x, error_top - 34, _font(19, bold=True))
        _text_center(draw, group, center, plot[3] + 15, _font(18))


def _stacked_outcome_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    rows: list[dict[str, Any]],
) -> None:
    _panel_header(draw, box, "(c)", "Outcome of Local-Only Updates")
    left, top, right, bottom = box
    entries = (("Success", "#E5E7EB", "diag"), ("Verification Failure / Rollback", "#94A3B8", "cross"))
    for index, (label, color, hatch) in enumerate(entries):
        x = left + 115 + index * 330
        _bar(draw, (x, top + 78, x + 30, top + 102), color, hatch)
        draw.text((x + 42, top + 74), label, fill=INK, font=_font(19))
    plot = (left + 100, top + 130, right - 35, bottom - 100)
    _axes(draw, plot, y_max=100.0, y_label="Run Outcome (%)", x_label="Removed Agents (%)", percent=True)
    ratios = sorted({float(row["removed_agents_percent"]) for row in rows})
    width = min(100, (plot[2] - plot[0]) / (len(ratios) * 2.0))
    for index, ratio in enumerate(ratios):
        x = plot[0] + (index + 0.5) * (plot[2] - plot[0]) / len(ratios)
        cursor = plot[3]
        for label, color, hatch in entries:
            value = float(next(row["percent"] for row in rows if row["removed_agents_percent"] == ratio and row["outcome"] == label))
            height = value / 100.0 * (plot[3] - plot[1])
            _bar(draw, (x - width / 2, cursor - height, x + width / 2, cursor), color, hatch)
            if value >= 12.0:
                _text_center(draw, f"{value:.0f}%", x, cursor - height / 2 - 13, _font(20, bold=True))
            cursor -= height
        _text_center(draw, f"{ratio:g}", x, plot[3] + 15, _font(22))


def _save(image: Image.Image, output_dir: Path, stem: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    png = output_dir / f"{stem}.png"
    pdf = output_dir / f"{stem}.pdf"
    image.save(png, dpi=(300, 300), optimize=True)
    image.convert("RGB").save(pdf, "PDF", resolution=300.0)
    return pdf, png


def generate_fig1(panel_a, panel_b, output_dir: Path) -> tuple[Path, Path]:
    image = Image.new("RGB", (2400, 1000), BACKGROUND)
    draw = ImageDraw.Draw(image)
    _line_panel(
        draw,
        (0, 0, 1200, 970),
        panel_a,
        panel_label="(a)",
        title="Transactionally Verified Formation Latency vs Task Size",
        x_key="num_agents",
        series_key="series",
        mean_key="mean_ms",
        ci_key="ci95_ms",
        x_label="Number of Business Agents",
        y_label="Latency (ms)",
        series_order=["t_form", "t_ctrl"],
        scope_note="In-memory transactional verification; no ping/iPerf3",
    )
    _stacked_breakdown_panel(
        draw,
        (1200, 0, 2400, 970),
        panel_b,
        panel_label="(b)",
        title="Formation Latency Breakdown",
        group_key="task_size",
        component_order=[
            "Mapping",
            "Cross-Layer Coordination",
            "Compilation",
            "Installation",
            "Activation",
            "Verification",
        ],
        task_groups=["Small", "Medium", "Large"],
        y_label="Latency (ms)",
        x_label="Task Size",
    )
    return _save(image, output_dir, "Fig1_Initial_Formation")


def generate_fig2(panels, output_dir: Path) -> tuple[Path, Path]:
    application, transport, physical, noise, scale = panels
    image = Image.new("RGB", (3000, 1840), BACKGROUND)
    draw = ImageDraw.Draw(image)
    common = dict(
        series_key="method",
        mean_key="mean_percent",
        ci_key="ci95_percent",
        y_label="QoS Satisfaction Rate (%)",
        series_order=["proposed", "adjacent", "independent", "no_verification"],
        y_max=100.0,
        percent=True,
    )
    _line_panel(
        draw,
        (0, 0, 1000, 900),
        application,
        panel_label="(a1)",
        title="QoS Satisfaction under Application–Capacity Conflicts",
        x_key="x",
        x_label="Conflict Pressure",
        **common,
    )
    _line_panel(
        draw,
        (1000, 0, 2000, 900),
        transport,
        panel_label="(a2)",
        title="QoS Satisfaction under Transport–Network Conflicts",
        x_key="x",
        x_label="Network Utilization (%)",
        x_formatter=lambda value: f"{value:g}",
        **common,
    )
    _line_panel(
        draw,
        (2000, 0, 3000, 900),
        physical,
        panel_label="(a3)",
        title="QoS Satisfaction under Network–Physical Conflicts",
        x_key="x",
        x_label="Physical Capacity Degradation (%)",
        x_formatter=lambda value: f"{value:g}",
        **common,
    )
    _line_panel(
        draw,
        (0, 900, 1500, 1840),
        noise,
        panel_label="(b)",
        title="Robustness to Observation Noise",
        x_key="x",
        series_key="method",
        mean_key="mean_percent",
        ci_key="ci95_percent",
        x_label="Observation Noise (%)",
        y_label="QoS Satisfaction Rate (%)",
        series_order=["proposed", "adjacent"],
        y_max=100.0,
        percent=True,
    )
    scale_plot = [
        {**row, "series": row["method"]}
        for row in scale
    ]
    original = METHOD_STYLES["proposed"]
    METHOD_STYLES["proposed"] = MethodStyle("Proposed Exact Search", original.color, original.line_pattern, original.marker, original.hatch)
    _line_panel(
        draw,
        (1500, 900, 3000, 1840),
        scale_plot,
        panel_label="(c)",
        title="Coordination Latency vs Number of Proposals",
        x_key="x",
        series_key="series",
        mean_key="mean_ms",
        ci_key="ci95_ms",
        x_label="Proposals per Layer (combinations: 1/16/81/256/625)",
        y_label="Coordination Latency (ms)",
        series_order=["proposed", "adjacent"],
    )
    METHOD_STYLES["proposed"] = original
    return _save(image, output_dir, "Fig2_Cross_Layer_Coordination")


def generate_fig3(panels, output_dir: Path) -> tuple[Path, Path]:
    latency, changed, local = panels
    image = Image.new("RGB", (3000, 1000), BACKGROUND)
    draw = ImageDraw.Draw(image)
    _line_panel(
        draw,
        (0, 0, 1000, 970),
        latency,
        panel_label="(a)",
        title="Elastic Expansion Latency vs Task Size",
        x_key="x",
        series_key="method",
        mean_key="mean_ms",
        ci_key="ci95",
        x_label="Number of Business Agents",
        y_label="Elastic Expansion Latency (ms)",
        series_order=["proposed", "full_rebuild"],
    )
    changed_plot = [
        {**row, "x": row["x_percent"], "mean": row["mean_ratio"], "error": row["ci95_ratio"]}
        for row in changed
    ]
    _line_panel(
        draw,
        (1000, 0, 2000, 970),
        changed_plot,
        panel_label="(b)",
        title="Changed-Rule Ratio vs Removed Agents",
        x_key="x",
        series_key="method",
        mean_key="mean",
        ci_key="error",
        x_label="Removed Agents (%)",
        y_label="Changed-Rule Ratio",
        series_order=["proposed", "full_rebuild"],
    )
    _stacked_outcome_panel(draw, (2000, 0, 3000, 970), local)
    return _save(image, output_dir, "Fig3_Business_Elasticity")


def generate_fig4(panels, output_dir: Path) -> tuple[Path, Path]:
    success, rules, breakdown = panels
    image = Image.new("RGB", (3000, 1100), BACKGROUND)
    draw = ImageDraw.Draw(image)
    groups = ["Agent Failure", "Link Failure", "Physical Capacity Drop"]
    _grouped_bar_panel(
        draw,
        (0, 0, 1000, 1070),
        success,
        panel_label="(a)",
        title="Recovery Success under Different Failures",
        group_key="paper_fault_family",
        groups=groups,
        series_order=["proposed", "full_rebuild", "network_only"],
        mean_key="mean_percent",
        ci_key="ci95_percent",
        y_label="Recovery Success Rate (%)",
        x_label="Failure Type",
        y_max=100.0,
        percent=True,
    )
    _grouped_bar_panel(
        draw,
        (1000, 0, 2000, 1070),
        rules,
        panel_label="(b)",
        title="Changed-Rule Ratio under Different Failures",
        group_key="paper_fault_family",
        groups=groups,
        series_order=["proposed", "full_rebuild"],
        mean_key="mean_ratio",
        ci_key="ci95_ratio",
        y_label="Changed-Rule Ratio",
        x_label="Failure Type",
        annotate_reduction=True,
    )
    _stacked_breakdown_panel(
        draw,
        (2000, 0, 3000, 1070),
        breakdown,
        panel_label="(c)",
        title="Repair Latency Breakdown",
        group_key="paper_fault_family",
        component_order=[
            "Localization",
            "Proposal",
            "Coordination",
            "Delta Compilation",
            "Stage",
            "Verification",
            "Activation",
        ],
        task_groups=groups,
        y_label="Repair Latency (ms)",
        x_label="P=Proposed, F=Full-Rebuild (detection excluded)",
        method_pairs=True,
    )
    return _save(image, output_dir, "Fig4_Failure_Reconfiguration")


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    return "\n".join(
        (
            "| " + " | ".join(headers) + " |",
            "|" + "|".join("---" for _ in headers) + "|",
            *("| " + " | ".join(row) + " |" for row in rows),
        )
    )


def write_summary(
    path: Path,
    fig1,
    fig2,
    fig3,
    fig4,
) -> None:
    formation, formation_breakdown = fig1
    application, transport, physical, noise, scale = fig2
    elastic, changed, local = fig3
    recovery, recovery_rules, repair = fig4
    lines = [
        "# WCNC paper result summary",
        "",
        "> Scope: all four figures are regenerated from raw experiment records. Exp1 is an "
        "in-memory transactionally verified control-plane simulation; it does not contain ping "
        "or iPerf3 and is therefore reported in milliseconds.",
        "",
        "## Initial formation",
        "",
    ]
    table = []
    for agents in sorted({int(row["num_agents"]) for row in formation}):
        form = next(row for row in formation if row["num_agents"] == agents and row["series"] == "t_form")
        ctrl = next(row for row in formation if row["num_agents"] == agents and row["series"] == "t_ctrl")
        share = float(form["verification_share_percent"])
        table.append([
            str(agents), f"{form['mean_ms']:.2f}", f"{ctrl['mean_ms']:.2f}",
            f"{share:.1f}%",
        ])
    lines.extend((_format_table(["Agents", "T_form (ms)", "T_ctrl (ms)", "Verification share"], table), ""))
    lines.extend(("## QoS satisfaction at the highest scanned conflict level", ""))
    conflict_rows = []
    for name, panel in (("Application–Capacity", application), ("Transport–Network", transport), ("Network–Physical", physical)):
        maximum = max(float(row["x"]) for row in panel)
        values = {row["method"]: row["mean_percent"] for row in panel if float(row["x"]) == maximum}
        conflict_rows.append([name, f"{maximum:g}", *(f"{values[m]:.1f}%" for m in ("proposed", "adjacent", "independent", "no_verification"))])
    lines.extend((_format_table(["Conflict", "Highest x", "Proposed", "Adjacent", "Independent", "w/o Verification"], conflict_rows), ""))
    lines.extend(("## Robustness and coordination scale", ""))
    noise20 = {row["method"]: row["mean_percent"] for row in noise if float(row["x"]) == 20.0}
    scale5 = {row["method"]: row["mean_ms"] for row in scale if int(row["x"]) == 5}
    lines.extend((
        f"- At 20% observation noise: Proposed {noise20['proposed']:.1f}% QoS satisfaction; "
        f"Adjacent-Layer {noise20['adjacent']:.1f}%.",
        f"- At 5 proposals/layer (625 raw combinations): Proposed exact search "
        f"{scale5['proposed']:.2f} ms; Adjacent-Layer {scale5['adjacent']:.2f} ms.",
        "",
        "## Business-change elasticity",
        "",
    ))
    elastic_rows = []
    for agents in sorted({int(row["x"]) for row in elastic}):
        values = {row["method"]: row["mean_ms"] for row in elastic if int(row["x"]) == agents}
        reduction = 100.0 * (1.0 - values["proposed"] / values["full_rebuild"])
        elastic_rows.append([str(agents), f"{values['proposed']:.2f}", f"{values['full_rebuild']:.2f}", f"{reduction:.1f}%"])
    lines.extend((_format_table(["Agents", "Proposed (ms)", "Full-Rebuild (ms)", "Latency reduction"], elastic_rows), ""))
    rule_rows = []
    for ratio in sorted({float(row["x_percent"]) for row in changed}):
        values = {row["method"]: row["mean_ratio"] for row in changed if float(row["x_percent"]) == ratio}
        reduction = 100.0 * (1.0 - values["proposed"] / values["full_rebuild"])
        local_failure = next(row["percent"] for row in local if row["removed_agents_percent"] == ratio and row["outcome"].startswith("Verification"))
        rule_rows.append([f"{ratio:g}%", f"{values['proposed']:.3f}", f"{values['full_rebuild']:.3f}", f"{reduction:.1f}%", f"{local_failure:.1f}%"])
    lines.extend((_format_table(["Removed", "Proposed rule ratio", "Full rule ratio", "Rule reduction", "Local-Only failure"], rule_rows), ""))
    lines.extend(("## Failure-driven recovery", ""))
    recovery_rows = []
    for family in ("Agent Failure", "Link Failure", "Physical Capacity Drop"):
        values = {row["method"]: row["mean_percent"] for row in recovery if row["paper_fault_family"] == family}
        rules = {row["method"]: row["mean_ratio"] for row in recovery_rules if row["paper_fault_family"] == family}
        rule_reduction = 100.0 * (1.0 - rules["proposed"] / rules["full_rebuild"])
        repair_totals = {
            method: sum(float(row["mean_ms"]) for row in repair if row["paper_fault_family"] == family and row["method"] == method)
            for method in ("proposed", "full_rebuild")
        }
        recovery_rows.append([
            family,
            f"{values['proposed']:.1f}%",
            f"{values['full_rebuild']:.1f}%",
            f"{values['network_only']:.1f}%",
            f"{rule_reduction:.1f}%",
            f"{repair_totals['proposed']:.2f} / {repair_totals['full_rebuild']:.2f}",
        ])
    lines.extend((_format_table(["Failure", "Proposed", "Full-Rebuild", "Network-Only", "Rule reduction", "Repair ms (P/F)"], recovery_rows), ""))
    lines.extend((
        "The common 150 ms detection interval is excluded from the repair breakdown. "
        "Verification includes post-activation stable-health windows so each stack closes to "
        "the measured post-detection repair latency.",
        "",
    ))
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def generate_all(
    *,
    exp1_raw: Path,
    exp2_raw: Path,
    robustness_raw: Path,
    exp3_raw: Path,
    exp4_raw: Path,
    output_dir: Path,
) -> list[Path]:
    data_dir = output_dir / "data"
    fig1 = build_fig1_data(exp1_raw)
    fig2 = build_fig2_data(exp2_raw, robustness_raw)
    fig3 = build_fig3_data(exp3_raw)
    fig4 = build_fig4_data(exp4_raw)
    for name, rows in (
        ("Fig1_a_formation_latency.csv", fig1[0]),
        ("Fig1_b_formation_breakdown.csv", fig1[1]),
        ("Fig2_a1_application_capacity.csv", fig2[0]),
        ("Fig2_a2_transport_network.csv", fig2[1]),
        ("Fig2_a3_network_physical.csv", fig2[2]),
        ("Fig2_b_noise_robustness.csv", fig2[3]),
        ("Fig2_c_proposal_scale.csv", fig2[4]),
        ("Fig3_a_elastic_latency.csv", fig3[0]),
        ("Fig3_b_changed_rule_ratio.csv", fig3[1]),
        ("Fig3_c_local_only_outcomes.csv", fig3[2]),
        ("Fig4_a_recovery_success.csv", fig4[0]),
        ("Fig4_b_changed_rule_ratio.csv", fig4[1]),
        ("Fig4_c_repair_breakdown.csv", fig4[2]),
    ):
        _write_csv(data_dir / name, rows)
    outputs = [*generate_fig1(*fig1, output_dir), *generate_fig2(fig2, output_dir), *generate_fig3(fig3, output_dir), *generate_fig4(fig4, output_dir)]
    summary = output_dir / "paper_result_summary.md"
    write_summary(summary, fig1, fig2, fig3, fig4)
    outputs.append(summary)
    return outputs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate final WCNC paper figures")
    parser.add_argument("--exp1", default="results/exp1/raw/runs.csv")
    parser.add_argument("--exp2", default="results/exp2/raw/runs.csv")
    parser.add_argument("--robustness", default="results/exp2_robustness/raw/runs.csv")
    parser.add_argument("--exp3", default="results/exp3/raw/runs.csv")
    parser.add_argument("--exp4", default="results/exp4/raw/runs.csv")
    parser.add_argument("--output-dir", default="results/paper_figures")
    return parser


def main() -> None:
    args = _parser().parse_args()
    outputs = generate_all(
        exp1_raw=Path(args.exp1),
        exp2_raw=Path(args.exp2),
        robustness_raw=Path(args.robustness),
        exp3_raw=Path(args.exp3),
        exp4_raw=Path(args.exp4),
        output_dir=Path(args.output_dir),
    )
    print(f"Wrote {len(outputs)} paper artifacts to {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
