from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.paper_protocol import EXPERIMENT_METHODS
from src.metrics.paper import PAPER_TRIAL_FIELDS


@dataclass(frozen=True)
class SanityFinding:
    level: str
    code: str
    message: str
    experiment: str
    series: str = ""
    method_id: str = ""


def check_results(
    source: str | Path | Sequence[Mapping[str, Any] | object],
    *,
    experiment: str | None = None,
) -> list[SanityFinding]:
    rows = _read_rows(source)
    if experiment is not None:
        rows = [row for row in rows if _text(row.get("experiment")) == experiment]
    selected_experiment = experiment or (
        _text(rows[0].get("experiment")) if rows else "unknown"
    )
    findings: list[SanityFinding] = []
    if not rows:
        return [
            SanityFinding(
                "ERROR",
                "EMPTY_RESULTS",
                "no trial rows were available for checking",
                selected_experiment,
            )
        ]

    missing_columns = sorted(set(PAPER_TRIAL_FIELDS) - set(rows[0]))
    if missing_columns:
        findings.append(
            SanityFinding(
                "ERROR",
                "MISSING_COLUMNS",
                "canonical raw columns are missing: " + ", ".join(missing_columns),
                selected_experiment,
            )
        )

    expected_methods = set(EXPERIMENT_METHODS.get(selected_experiment, ()))
    for trial_id, paired in _group(rows, "trial_id").items():
        actual_methods = {_text(row.get("method_id")) for row in paired}
        if expected_methods and actual_methods != expected_methods:
            findings.append(
                SanityFinding(
                    "ERROR",
                    "INCOMPLETE_METHOD_PAIR",
                    f"{trial_id} has {sorted(actual_methods)}, expected {sorted(expected_methods)}",
                    selected_experiment,
                    _text(paired[0].get("series")),
                )
            )
        for field in (
            "topology_fingerprint",
            "scenario_fingerprint",
            "qos_fingerprint",
            "event_fingerprint",
        ):
            if len({_text(row.get(field)) for row in paired}) != 1:
                findings.append(
                    SanityFinding(
                        "ERROR",
                        "MISMATCHED_PAIR_FINGERPRINT",
                        f"{trial_id} does not share one {field}",
                        selected_experiment,
                        _text(paired[0].get("series")),
                    )
                )

    latency_field = {
        "exp1": "formation_latency_ms",
        "exp2": "resolution_latency_ms",
        "exp3": "reconfiguration_latency_ms",
        "exp4": "recovery_latency_ms",
    }.get(selected_experiment)
    if latency_field is not None:
        for row in rows:
            numeric = _optional_number(row.get(latency_field))
            if numeric is not None and numeric <= 0.0:
                findings.append(
                    SanityFinding(
                        "ERROR",
                        "ZERO_LATENCY",
                        f"{latency_field} must be positive for {_text(row.get('trial_id'))}",
                        selected_experiment,
                        _text(row.get("series")),
                        _text(row.get("method_id")),
                    )
                )

    ratio_fields = (
        "state_churn_probability",
        "conflict_density",
        "affected_scope_ratio",
        "failure_severity",
        "rule_change_ratio",
        "gateway_change_ratio",
        "unaffected_disturbance_ratio",
    )
    for row in rows:
        for field in ratio_fields:
            numeric = _optional_number(row.get(field))
            if numeric is not None and not 0.0 <= numeric <= 1.0:
                findings.append(
                    SanityFinding(
                        "ERROR",
                        "IMPOSSIBLE_RATIO",
                        f"{field}={numeric:g} is outside [0, 1]",
                        selected_experiment,
                        _text(row.get("series")),
                        _text(row.get("method_id")),
                    )
                )

    findings.extend(_constant_output_findings(rows, selected_experiment, latency_field))
    if selected_experiment == "exp1":
        findings.extend(_exp1_trend_findings(rows))
    return _deduplicate(findings)


def _constant_output_findings(
    rows: Sequence[dict[str, Any]],
    experiment: str,
    latency_field: str | None,
) -> list[SanityFinding]:
    findings: list[SanityFinding] = []
    for (series, method_id), selected in _group_multi(
        rows,
        ("series", "method_id"),
    ).items():
        x_values = {
            _series_x_value(row)
            for row in selected
            if _series_x_value(row) is not None
        }
        if len(x_values) < 2:
            continue
        primary_latency_series = {
            "exp1": {"task_size"},
            "exp2": set(),
            "exp3": {"affected_scope"},
            "exp4": {"failure_type"},
        }.get(experiment, set())
        if latency_field is not None and series in primary_latency_series:
            latencies = {
                value
                for row in selected
                if (value := _optional_number(row.get(latency_field))) is not None
            }
            if len(latencies) == 1:
                findings.append(
                    SanityFinding(
                        "ERROR",
                        "CONSTANT_LATENCY",
                        f"{method_id} returns constant {latency_field} across {series}",
                        experiment,
                        series,
                        method_id,
                    )
                )
        primary_rate_series = {
            "exp1": {"state_churn"},
            "exp2": {"conflict_density"},
            "exp3": {"affected_scope"},
            "exp4": {"failure_type", "capacity_stress"},
        }.get(experiment, set())
        successes = (
            [_optional_boolean(row.get("success")) for row in selected]
            if series in primary_rate_series
            else []
        )
        successes = [value for value in successes if value is not None]
        if method_id != "proposed" and successes and all(successes):
            findings.append(
                SanityFinding(
                    "WARNING",
                    "ALWAYS_SUCCESS",
                    f"{method_id} succeeds for every sampled {series} trial",
                    experiment,
                    series,
                    method_id,
                )
            )
        if method_id != "proposed" and successes and not any(successes):
            findings.append(
                SanityFinding(
                    "WARNING",
                    "ALWAYS_FAILURE",
                    f"{method_id} fails every sampled {series} trial",
                    experiment,
                    series,
                    method_id,
                )
            )
    return findings


def _exp1_trend_findings(rows: Sequence[dict[str, Any]]) -> list[SanityFinding]:
    findings: list[SanityFinding] = []
    task_rows = [row for row in rows if _text(row.get("series")) == "task_size"]
    for method_id, selected in _group(task_rows, "method_id").items():
        means = []
        for task_size, point_rows in _group(selected, "task_size").items():
            values = [
                value
                for row in point_rows
                if (value := _optional_number(row.get("formation_latency_ms"))) is not None
            ]
            if values:
                means.append((float(task_size), mean(values)))
        means.sort()
        if len(means) >= 2:
            nondecreasing = sum(
                right[1] >= left[1]
                for left, right in zip(means, means[1:])
            )
            comparisons = len(means) - 1
            if means[-1][1] <= means[0][1] or nondecreasing / comparisons < 0.60:
                findings.append(
                    SanityFinding(
                        "WARNING",
                        "EXP1_LATENCY_NOT_INCREASING",
                        f"{method_id} formation latency does not generally rise with task size",
                        "exp1",
                        "task_size",
                        method_id,
                    )
                )

    churn_rows = [row for row in rows if _text(row.get("series")) == "state_churn"]
    for probability, point_rows in _group(churn_rows, "state_churn_probability").items():
        numeric_probability = _optional_number(probability)
        if numeric_probability is None or numeric_probability <= 0.0:
            continue
        rates = []
        for method_rows in _group(point_rows, "method_id").values():
            successes = [
                value
                for row in method_rows
                if (value := _optional_boolean(row.get("success"))) is not None
            ]
            if successes:
                rates.append(sum(successes) / len(successes))
        if rates and all(rate >= 1.0 for rate in rates):
            findings.append(
                SanityFinding(
                    "WARNING",
                    "SCENARIO_MAY_BE_TOO_EASY",
                    f"all methods succeed at nonzero churn probability {numeric_probability:g}",
                    "exp1",
                    "state_churn",
                )
            )
        if rates and all(rate < 0.20 for rate in rates):
            findings.append(
                SanityFinding(
                    "WARNING",
                    "SCENARIO_MAY_BE_TOO_DIFFICULT",
                    f"all methods have success below 20% at churn probability {numeric_probability:g}",
                    "exp1",
                    "state_churn",
                )
            )
    return findings


def _read_rows(
    source: str | Path | Sequence[Mapping[str, Any] | object],
) -> list[dict[str, Any]]:
    if isinstance(source, (str, Path)):
        with Path(source).open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    materialized = []
    for row in source:
        if is_dataclass(row):
            materialized.append(asdict(row))
        else:
            materialized.append(dict(row))
    return materialized


def _group(
    rows: Iterable[dict[str, Any]],
    field: str,
) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.get(field), []).append(row)
    return grouped


def _group_multi(
    rows: Iterable[dict[str, Any]],
    fields: tuple[str, ...],
) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        key = tuple(_text(row.get(field)) for field in fields)
        grouped.setdefault(key, []).append(row)
    return grouped


def _series_x_value(row: Mapping[str, Any]) -> float | None:
    series = _text(row.get("series"))
    field = {
        "task_size": "task_size",
        "state_churn": "state_churn_probability",
        "conflict_density": "conflict_density",
        "affected_scope": "affected_scope_ratio",
        "capacity_stress": "failure_severity",
    }.get(series)
    return _optional_number(row.get(field)) if field is not None else None


def _optional_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = _text(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _deduplicate(findings: Iterable[SanityFinding]) -> list[SanityFinding]:
    return sorted(
        set(findings),
        key=lambda item: (
            {"ERROR": 0, "WARNING": 1, "INFO": 2}.get(item.level, 3),
            item.code,
            item.series,
            item.method_id,
            item.message,
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check canonical paper trial results")
    parser.add_argument("--input", required=True)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--output")
    return parser


def main() -> None:
    args = _parser().parse_args()
    findings = check_results(args.input, experiment=args.experiment)
    payload = [asdict(item) for item in findings]
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    for finding in findings:
        print(f"[{finding.level}] {finding.code}: {finding.message}")
    if any(item.level == "ERROR" for item in findings):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
