from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MetricSnapshot:
    timestamp: float
    app_rate_mbps: float
    rtt_ms: float
    retransmission_rate: float
    send_rate_mbps: float
    available_bandwidth_mbps: float
    throughput_mbps: float
    latency_ms: float
    jitter_ms: float
    loss_rate: float
    utilization: float
    queue_backlog: float
    phy_stability: float = 1.0


class MetricProvider(Protocol):
    def snapshot(self, task_id: str, agent_id: str, timestamp: float) -> MetricSnapshot:
        """Return the current metrics for an agent in a task."""

    def apply_effect(self, task_id: str, effect: dict[str, float]) -> None:
        """Apply simulated feedback from an agent action."""

    def inject_event(self, task_id: str, event_type: str, severity: float = 1.0) -> None:
        """Inject a scenario event such as bearer degradation or agent failure."""

