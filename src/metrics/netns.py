from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

from src.metrics.parsers import parse_iperf3_json, parse_ping, parse_ss_ti
from src.metrics.provider import MetricSnapshot


@dataclass(frozen=True)
class NetnsTarget:
    namespace: str
    target_ip: str
    iperf_port: int = 5201
    ping_count: int = 5
    ping_interval_s: float = 0.2
    iperf_seconds: int = 1
    command_timeout_s: float = 8.0
    sudo: bool = False


@dataclass
class NetnsMetricProvider:
    targets: dict[str, NetnsTarget]
    default_target: NetnsTarget
    app_rate_mbps: float = 16.0
    cache_ttl_s: float = 1.0
    _cache: dict[str, tuple[float, MetricSnapshot]] = field(default_factory=dict)
    _effects: dict[str, dict[str, float]] = field(default_factory=dict)

    def snapshot(self, task_id: str, agent_id: str, timestamp: float) -> MetricSnapshot:
        target = self.targets.get(agent_id, self.default_target)
        cache_key = f"{task_id}:{target.namespace}:{target.target_ip}:{target.iperf_port}"
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and now - cached[0] <= self.cache_ttl_s:
            return cached[1]

        snapshot = self._measure(task_id, timestamp, target)
        self._cache[cache_key] = (now, snapshot)
        return snapshot

    def apply_effect(self, task_id: str, effect: dict[str, float]) -> None:
        current = self._effects.setdefault(task_id, {})
        for key, value in effect.items():
            if key.endswith("_multiplier"):
                current[key] = current.get(key, 1.0) * value
            else:
                current[key] = current.get(key, 0.0) + value
        self._cache.clear()

    def inject_event(self, task_id: str, event_type: str, severity: float = 1.0) -> None:
        # Real event injection is handled by tc/netem scripts. This method exists
        # to keep the MetricProvider interface compatible with mock mode.
        self._cache.clear()

    def _measure(self, task_id: str, timestamp: float, target: NetnsTarget) -> MetricSnapshot:
        ping = parse_ping(
            self._run(
                target,
                "ping",
                "-c",
                str(target.ping_count),
                "-i",
                f"{target.ping_interval_s:g}",
                "-W",
                "2",
                target.target_ip,
            )
        )
        iperf = parse_iperf3_json(
            self._run(
                target,
                "iperf3",
                "-c",
                target.target_ip,
                "-p",
                str(target.iperf_port),
                "-t",
                str(target.iperf_seconds),
                "-J",
            )
        )
        ss = parse_ss_ti(
            self._run(
                target,
                "ss",
                "-tin",
                "dst",
                target.target_ip,
                allow_failure=True,
            )
        )

        effects = self._effects.setdefault(task_id, {})
        app_rate = self.app_rate_mbps * effects.get("app_rate_multiplier", 1.0)
        throughput = iperf.throughput_mbps
        available = max(0.0, throughput + effects.get("bandwidth_delta", 0.0))
        loss = max(ping.loss_rate, ss.retransmission_rate)
        latency = ss.srtt_ms or ping.rtt_avg_ms
        send_rate = ss.delivery_rate_mbps or iperf.sender_mbps or app_rate
        utilization = min(1.0, app_rate / max(available, 0.001))

        return MetricSnapshot(
            timestamp=timestamp,
            app_rate_mbps=app_rate,
            rtt_ms=latency,
            retransmission_rate=loss,
            send_rate_mbps=send_rate,
            available_bandwidth_mbps=available,
            throughput_mbps=throughput,
            latency_ms=ping.rtt_avg_ms,
            jitter_ms=ping.jitter_ms or ss.rtt_var_ms,
            loss_rate=ping.loss_rate,
            utilization=utilization,
            queue_backlog=max(0.0, utilization - 0.72) * 100.0,
        )

    @staticmethod
    def _run(
        target: NetnsTarget,
        *args: str,
        allow_failure: bool = False,
    ) -> str:
        command = ["ip", "netns", "exec", target.namespace, *args]
        if target.sudo:
            command = ["sudo", *command]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=target.command_timeout_s,
        )
        if completed.returncode != 0 and allow_failure:
            return completed.stdout
        if completed.returncode != 0:
            raise RuntimeError(
                "command failed: "
                + " ".join(command)
                + f"\nexit={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"
            )
        return completed.stdout
