from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from src.core.models import SessionSpec, TaskSubnet
from src.e2e.models import EdgeVerifyResult, VerifyResult
from src.metrics.netns import NetnsMetricProvider
from src.metrics.provider import MetricProvider, MetricSnapshot
from src.metrics.synthetic import SyntheticMetricProvider


class TaskSubnetVerifier(Protocol):
    async def verify(self, subnet: TaskSubnet) -> VerifyResult:
        ...


@dataclass
class _MetricTaskSubnetVerifier:
    provider: MetricProvider
    mode: str
    probe_delay_ms: float = 0.0
    threaded: bool = False
    timestamp: float = 0.0

    async def verify(self, subnet: TaskSubnet) -> VerifyResult:
        started = perf_counter()
        results = tuple(
            await asyncio.gather(
                *(self._verify_session(subnet, session) for session in subnet.sessions)
            )
        )
        elapsed_ms = (perf_counter() - started) * 1000.0
        passed = sum(1 for item in results if item.ok)
        self.timestamp += 1.0
        return VerifyResult(
            ok=passed == len(results) and bool(results),
            verify_ms=elapsed_ms,
            checked_edges=len(results),
            passed_edges=passed,
            edge_results=results,
            mode=self.mode,
        )

    async def _verify_session(
        self,
        subnet: TaskSubnet,
        session: SessionSpec,
    ) -> EdgeVerifyResult:
        if self.probe_delay_ms > 0:
            await asyncio.sleep(self.probe_delay_ms / 1000.0)
        try:
            if self.threaded:
                async_snapshot = getattr(self.provider, "snapshot_async", None)
                if async_snapshot is None:
                    snapshot = self.provider.snapshot(
                        subnet.task.task_id,
                        session.n_agent_id,
                        self.timestamp,
                    )
                else:
                    snapshot = await async_snapshot(
                        subnet.task.task_id,
                        session.n_agent_id,
                        self.timestamp,
                    )
            else:
                snapshot = self.provider.snapshot(
                    subnet.task.task_id,
                    session.n_agent_id,
                    self.timestamp,
                )
            return _evaluate_snapshot(subnet, session, snapshot)
        except Exception as error:  # command failures become auditable edge failures
            return EdgeVerifyResult(
                session_id=session.session_id,
                source=session.source,
                target=session.target,
                ok=False,
                reachable=False,
                max_latency_ms=session.latency_budget_ms,
                max_loss_rate=subnet.task.qos.max_loss_rate,
                min_bandwidth_mbps=session.data_rate_mbps,
                error=str(error),
            )


class SyntheticTaskSubnetVerifier(_MetricTaskSubnetVerifier):
    def __init__(
        self,
        provider: SyntheticMetricProvider,
        *,
        probe_delay_ms: float = 10.0,
    ) -> None:
        super().__init__(
            provider=provider,
            mode="synthetic",
            probe_delay_ms=probe_delay_ms,
            threaded=False,
        )


class NetnsTaskSubnetVerifier(_MetricTaskSubnetVerifier):
    def __init__(self, provider: NetnsMetricProvider) -> None:
        # Each business edge is measured independently. Disabling the cache is
        # important for recovery loops, where every window must be a new probe.
        provider.cache_ttl_s = 0.0
        super().__init__(provider=provider, mode="netns", threaded=True)


def _evaluate_snapshot(
    subnet: TaskSubnet,
    session: SessionSpec,
    snapshot: MetricSnapshot,
) -> EdgeVerifyResult:
    latency_ok = snapshot.latency_ms <= session.latency_budget_ms
    loss_ok = snapshot.loss_rate <= subnet.task.qos.max_loss_rate
    available_ok = snapshot.available_bandwidth_mbps >= session.data_rate_mbps
    throughput_ok = snapshot.throughput_mbps >= session.data_rate_mbps
    reachable = snapshot.throughput_mbps > 0.0
    ok = reachable and latency_ok and loss_ok and available_ok and throughput_ok
    return EdgeVerifyResult(
        session_id=session.session_id,
        source=session.source,
        target=session.target,
        ok=ok,
        reachable=reachable,
        latency_ms=snapshot.latency_ms,
        max_latency_ms=session.latency_budget_ms,
        loss_rate=snapshot.loss_rate,
        max_loss_rate=subnet.task.qos.max_loss_rate,
        available_bandwidth_mbps=snapshot.available_bandwidth_mbps,
        min_bandwidth_mbps=session.data_rate_mbps,
        throughput_mbps=snapshot.throughput_mbps,
    )
