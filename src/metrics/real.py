from __future__ import annotations

from src.metrics.provider import MetricSnapshot


class RealMetricProvider:
    """Placeholder for ns-3, Mininet, or real measurement adapters in P5."""

    def snapshot(self, task_id: str, agent_id: str, timestamp: float) -> MetricSnapshot:
        raise NotImplementedError("RealMetricProvider is reserved for P5.")

    def apply_effect(self, task_id: str, effect: dict[str, float]) -> None:
        raise NotImplementedError("RealMetricProvider is reserved for P5.")

    def inject_event(self, task_id: str, event_type: str, severity: float = 1.0) -> None:
        raise NotImplementedError("RealMetricProvider is reserved for P5.")

