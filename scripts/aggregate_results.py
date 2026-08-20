from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.paper_protocol import stable_fingerprint
from scripts.paper_statistics import cluster_bootstrap_interval


@dataclass(frozen=True)
class AggregateRow:
    experiment: str
    mode: str
    series: str
    method_id: str
    method_label: str
    x_name: str
    x_value: float
    metric: str
    mean: float
    p50: float
    p95: float
    ci_lower: float
    ci_upper: float
    sample_count: int
    cluster_count: int
    bootstrap_iterations: int
    numerator: int | None
    denominator: int | None


AGGREGATE_FIELDS = tuple(item.name for item in fields(AggregateRow))


def aggregate_experiment(
    source: str | Path | Sequence[Mapping[str, Any]],
    output_csv: str | Path,
    *,
    bootstrap_iterations: int = 5000,
) -> list[AggregateRow]:
    rows = _read_rows(source)
    if not rows:
        raise ValueError("cannot aggregate an empty paper result")
    experiments = {_text(row.get("experiment")) for row in rows}
    if len(experiments) != 1 or not experiments <= {"exp1", "exp2", "exp3", "exp4"}:
        raise ValueError(
            "aggregation input must contain one supported canonical experiment"
        )

    grouped: dict[tuple[str, str, str, str, float, str], list[dict[str, Any]]] = {}
    for row in rows:
        experiment = _text(row.get("experiment"))
        series = _text(row.get("series"))
        x_name, x_value, metric_values = _row_aggregate_values(
            row,
            experiment,
            series,
        )
        for metric, value in metric_values:
            key = (
                experiment,
                _text(row.get("mode")),
                series,
                _text(row.get("method_id")),
                x_value,
                metric,
            )
            grouped.setdefault(key, []).append(
                {
                    **row,
                    "_aggregate_value": value,
                    "seed": int(row["seed"]),
                    "_x_name": x_name,
                }
            )

    summary: list[AggregateRow] = []
    for key, group in sorted(grouped.items()):
        experiment, mode, series, method_id, x_value, metric = key
        values = np.asarray(
            [float(row["_aggregate_value"]) for row in group],
            dtype=float,
        )
        bootstrap_seed = int(
            stable_fingerprint(
                {
                    "experiment": experiment,
                    "mode": mode,
                    "series": series,
                    "method_id": method_id,
                    "x_value": x_value,
                    "metric": metric,
                }
            )[:16],
            16,
        )
        interval = cluster_bootstrap_interval(
            group,
            value="_aggregate_value",
            cluster="seed",
            statistic=np.mean,
            iterations=bootstrap_iterations,
            seed=bootstrap_seed,
        )
        rate_metric = metric in {
            "success_rate_percent",
            "qos_recovery_rate_percent",
            "feasible_solution_rate_percent",
            "qos_satisfaction_rate_percent",
            "safe_rejection_rate_percent",
        }
        summary.append(
            AggregateRow(
                experiment=experiment,
                mode=mode,
                series=series,
                method_id=method_id,
                method_label=_text(group[0].get("method_label")),
                x_name=_text(group[0]["_x_name"]),
                x_value=x_value,
                metric=metric,
                mean=interval.estimate,
                p50=float(np.percentile(values, 50.0)),
                p95=float(np.percentile(values, 95.0)),
                ci_lower=interval.lower,
                ci_upper=interval.upper,
                sample_count=interval.sample_count,
                cluster_count=interval.cluster_count,
                bootstrap_iterations=interval.iterations,
                numerator=(
                    sum(value > 50.0 for value in values) if rate_metric else None
                ),
                denominator=(len(values) if rate_metric else None),
            )
        )

    _write_summary(Path(output_csv), summary)
    return summary


def _row_aggregate_values(
    row: Mapping[str, Any],
    experiment: str,
    series: str,
) -> tuple[str, float, list[tuple[str, float]]]:
    if experiment == "exp1" and series == "task_size":
        metric = "formation_latency_ms"
        return (
            "task_size",
            _number(row.get("task_size"), "task_size"),
            [(metric, _number(row.get(metric), metric))],
        )
    if experiment == "exp1" and series == "state_churn":
        return (
            "state_churn_probability_percent",
            100.0
            * _number(
                row.get("state_churn_probability"),
                "state_churn_probability",
            ),
            [("success_rate_percent", 100.0 if _boolean(row.get("success")) else 0.0)],
        )
    if experiment == "exp2" and series == "conflict_density":
        x_value = 100.0 * _number(row.get("conflict_density"), "conflict_density")
        if _boolean(row.get("ground_truth_feasible")):
            return (
                "conflict_density_percent",
                x_value,
                [
                    (
                        "feasible_solution_rate_percent",
                        100.0 if _boolean(row.get("success")) else 0.0,
                    ),
                    (
                        "qos_satisfaction_rate_percent",
                        100.0 if _boolean(row.get("qos_satisfied")) else 0.0,
                    ),
                ],
            )
        return (
            "conflict_density_percent",
            x_value,
            [
                (
                    "safe_rejection_rate_percent",
                    100.0 if _boolean(row.get("safe_rejection")) else 0.0,
                )
            ],
        )
    if experiment == "exp3" and series == "affected_scope":
        x_value = _number(
            row.get("affected_scope_bucket_percent"),
            "affected_scope_bucket_percent",
        )
        values = [
            (
                "rule_change_ratio_percent",
                100.0 * _number(row.get("rule_change_ratio"), "rule_change_ratio"),
            ),
            (
                "gateway_change_ratio_percent",
                100.0
                * _number(row.get("gateway_change_ratio"), "gateway_change_ratio"),
            ),
            (
                "unaffected_disturbance_ratio_percent",
                100.0
                * _number(
                    row.get("unaffected_disturbance_ratio"),
                    "unaffected_disturbance_ratio",
                ),
            ),
            ("success_rate_percent", 100.0 if _boolean(row.get("success")) else 0.0),
        ]
        latency = row.get("reconfiguration_latency_ms")
        if latency not in (None, ""):
            values.append(
                (
                    "reconfiguration_latency_ms",
                    _number(latency, "reconfiguration_latency_ms"),
                )
            )
        return "affected_scope_ratio_percent", x_value, values
    if experiment == "exp4" and series in {"failure_type", "capacity_stress"}:
        if series == "failure_type":
            failure_indices = {
                "agent_failure": 0.0,
                "link_failure": 1.0,
                "capacity_degradation": 2.0,
            }
            failure_type = _text(row.get("failure_type"))
            if failure_type not in failure_indices:
                raise ValueError(f"unsupported Exp.4 failure type: {failure_type}")
            x_name = "failure_type_index"
            x_value = failure_indices[failure_type]
        else:
            x_name = "capacity_reduction_percent"
            x_value = 100.0 * _number(
                row.get("failure_severity"),
                "failure_severity",
            )
        values = [
            ("success_rate_percent", 100.0 if _boolean(row.get("success")) else 0.0),
            (
                "qos_recovery_rate_percent",
                100.0 if _boolean(row.get("qos_satisfied")) else 0.0,
            ),
            (
                "rule_change_ratio_percent",
                100.0 * _number(row.get("rule_change_ratio"), "rule_change_ratio"),
            ),
            (
                "gateway_change_ratio_percent",
                100.0
                * _number(row.get("gateway_change_ratio"), "gateway_change_ratio"),
            ),
            (
                "unaffected_disturbance_ratio_percent",
                100.0
                * _number(
                    row.get("unaffected_disturbance_ratio"),
                    "unaffected_disturbance_ratio",
                ),
            ),
        ]
        latency = row.get("recovery_latency_ms")
        if latency not in (None, ""):
            values.append(
                (
                    "recovery_latency_ms",
                    _number(latency, "recovery_latency_ms"),
                )
            )
        return x_name, x_value, values
    raise ValueError(f"unsupported aggregate series: {experiment}/{series}")


def _read_rows(
    source: str | Path | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(source, (str, Path)):
        with Path(source).open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    return [dict(row) for row in source]


def _write_summary(path: Path, rows: Iterable[AggregateRow]) -> None:
    materialized = [asdict(row) for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(AGGREGATE_FIELDS),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(materialized)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number(value: Any, field: str) -> float:
    if value in (None, ""):
        raise ValueError(f"required numeric aggregation field is empty: {field}")
    numeric = float(value)
    if not np.isfinite(numeric):
        raise ValueError(f"aggregation field must be finite: {field}")
    return numeric


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate canonical paper trials")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bootstrap-iterations", type=int, default=5000)
    return parser


def main() -> None:
    args = _parser().parse_args()
    rows = aggregate_experiment(
        args.input,
        args.output,
        bootstrap_iterations=args.bootstrap_iterations,
    )
    print(f"wrote {len(rows)} aggregate rows to {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
