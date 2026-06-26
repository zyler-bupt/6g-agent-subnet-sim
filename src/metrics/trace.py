from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from src.metrics.provider import MetricProvider, MetricSnapshot


@dataclass
class TraceMetricProvider:
    trace_path: str | Path
    base_provider: MetricProvider
    target_mean_mbps: float = 16.0
    sample_interval_s: float = 1.0

    def __post_init__(self) -> None:
        raw = np.load(self.trace_path).astype(np.float32).reshape(-1)
        if len(raw) == 0:
            raise ValueError("trace must not be empty")
        mean = float(raw.mean())
        scale = self.target_mean_mbps / mean if mean > 0 else 1.0
        self._rates = np.maximum(0.1, raw * scale).astype(np.float32)

    def snapshot(self, task_id: str, agent_id: str, timestamp: float) -> MetricSnapshot:
        base = self.base_provider.snapshot(task_id, agent_id, timestamp)
        rate = float(self._rates[self._index(timestamp)])
        return replace(base, app_rate_mbps=rate, send_rate_mbps=max(base.send_rate_mbps, rate))

    def apply_effect(self, task_id: str, effect: dict[str, float]) -> None:
        self.base_provider.apply_effect(task_id, effect)

    def inject_event(self, task_id: str, event_type: str, severity: float = 1.0) -> None:
        self.base_provider.inject_event(task_id, event_type, severity)

    def _index(self, timestamp: float) -> int:
        return int(max(0.0, timestamp) / self.sample_interval_s) % len(self._rates)
