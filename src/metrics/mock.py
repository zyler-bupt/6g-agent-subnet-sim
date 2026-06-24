from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from src.metrics.provider import MetricSnapshot


@dataclass
class MockMetricProvider:
    seed: int = 7
    base_bandwidth_mbps: float = 32.0
    base_latency_ms: float = 42.0
    base_app_rate_mbps: float = 16.0
    _effects: dict[str, dict[str, float]] = field(default_factory=dict)
    _events: dict[str, dict[str, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def snapshot(self, task_id: str, agent_id: str, timestamp: float) -> MetricSnapshot:
        effects = self._effects.setdefault(task_id, {})
        events = self._events.setdefault(task_id, {})
        wave = math.sin(timestamp / 2.5 + len(agent_id) * 0.17)
        noise = self._rng.uniform(-0.35, 0.35)
        bearer_drop = events.get("bearer_degradation", 0.0)
        qos_pressure = events.get("qos_change", 0.0)

        app_rate = max(0.5, self.base_app_rate_mbps + 2.5 * wave + qos_pressure)
        app_rate *= effects.get("app_rate_multiplier", 1.0)
        bandwidth = max(
            1.0,
            self.base_bandwidth_mbps
            - 18.0 * bearer_drop
            + effects.get("bandwidth_delta", 0.0)
            + noise,
        )
        latency = max(
            1.0,
            self.base_latency_ms + 38.0 * bearer_drop - effects.get("latency_delta", 0.0),
        )
        loss = min(
            0.35,
            max(0.0, 0.006 + 0.08 * bearer_drop - effects.get("loss_delta", 0.0)),
        )
        utilization = min(1.0, max(0.0, app_rate / max(bandwidth, 1.0)))
        return MetricSnapshot(
            timestamp=timestamp,
            app_rate_mbps=app_rate,
            rtt_ms=latency * 0.72,
            retransmission_rate=loss * 1.4,
            send_rate_mbps=app_rate,
            available_bandwidth_mbps=bandwidth,
            throughput_mbps=min(app_rate, bandwidth * (1.0 - loss)),
            latency_ms=latency,
            jitter_ms=3.0 + 8.0 * bearer_drop + abs(wave),
            loss_rate=loss,
            utilization=utilization,
            queue_backlog=max(0.0, utilization - 0.72) * 100.0,
        )

    def apply_effect(self, task_id: str, effect: dict[str, float]) -> None:
        current = self._effects.setdefault(task_id, {})
        for key, value in effect.items():
            if key.endswith("_multiplier"):
                current[key] = current.get(key, 1.0) * value
            else:
                current[key] = current.get(key, 0.0) + value

    def inject_event(self, task_id: str, event_type: str, severity: float = 1.0) -> None:
        current = self._events.setdefault(task_id, {})
        current[event_type] = max(current.get(event_type, 0.0), severity)

