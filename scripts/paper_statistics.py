from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class BootstrapInterval:
    estimate: float
    lower: float
    upper: float
    cluster_count: int
    sample_count: int
    iterations: int


def cluster_bootstrap_interval(
    rows: Sequence[Mapping[str, Any]],
    value: str,
    cluster: str,
    statistic: Callable[[Sequence[float]], float] = np.mean,
    iterations: int = 5000,
    seed: int = 0,
) -> BootstrapInterval:
    """Return a percentile CI after resampling complete topology clusters."""

    if iterations <= 0:
        raise ValueError("bootstrap iterations must be positive")
    grouped: dict[Any, list[float]] = {}
    for row in rows:
        if cluster not in row:
            raise KeyError(f"cluster column is missing: {cluster}")
        if value not in row:
            raise KeyError(f"value column is missing: {value}")
        numeric = float(row[value])
        if not np.isfinite(numeric):
            raise ValueError(f"bootstrap value must be finite: {numeric}")
        grouped.setdefault(row[cluster], []).append(numeric)
    if not grouped:
        raise ValueError("cluster bootstrap requires at least one row")

    cluster_ids = tuple(sorted(grouped, key=repr))
    materialized = [value for cluster_id in cluster_ids for value in grouped[cluster_id]]
    estimate = float(statistic(materialized))
    rng = np.random.default_rng(seed)
    bootstrap_values = np.empty(iterations, dtype=float)
    for index in range(iterations):
        sampled_indices = rng.integers(0, len(cluster_ids), size=len(cluster_ids))
        sampled_values = [
            item
            for sampled_index in sampled_indices
            for item in grouped[cluster_ids[int(sampled_index)]]
        ]
        bootstrap_values[index] = float(statistic(sampled_values))

    lower, upper = np.percentile(bootstrap_values, (2.5, 97.5))
    return BootstrapInterval(
        estimate=estimate,
        lower=min(float(lower), estimate),
        upper=max(float(upper), estimate),
        cluster_count=len(cluster_ids),
        sample_count=len(materialized),
        iterations=iterations,
    )
