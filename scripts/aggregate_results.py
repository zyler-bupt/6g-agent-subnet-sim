from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

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
    if experiments != {"exp1"}:
        raise ValueError(
            "current aggregation checkpoint accepts only canonical Exp.1 rows"
        )

    grouped: dict[tuple[str, str, str, str, float, str], list[dict[str, Any]]] = {}
    for row in rows:
        series = _text(row.get("series"))
        if series == "task_size":
            x_name = "task_size"
            x_value = _number(row.get("task_size"), "task_size")
            metric = "formation_latency_ms"
            value = _number(row.get("formation_latency_ms"), metric)
        elif series == "state_churn":
            x_name = "state_churn_probability_percent"
            x_value = 100.0 * _number(
                row.get("state_churn_probability"),
                "state_churn_probability",
            )
            metric = "success_rate_percent"
            value = 100.0 if _boolean(row.get("success")) else 0.0
        else:
            raise ValueError(f"unsupported Exp.1 series: {series}")
        key = (
            _text(row.get("experiment")),
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
            )
        )

    _write_summary(Path(output_csv), summary)
    return summary


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
