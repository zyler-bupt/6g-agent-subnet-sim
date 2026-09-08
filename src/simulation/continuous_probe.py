from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Callable

from src.core.models import SessionSpec, TaskSubnet
from src.metrics.provider import MetricProvider

if TYPE_CHECKING:
    from src.controller.networking import AgentController


@dataclass(frozen=True)
class BusinessProbeSample:
    edge_id: str
    probe_timestamp: float
    stage: str
    reachable: bool
    latency_ms: float
    packet_loss: float
    throughput_mbps: float


@dataclass(frozen=True)
class ContinuousProbeSummary:
    samples: tuple[BusinessProbeSample, ...]
    unaffected_edges_total: int
    unaffected_edges_interrupted: int
    unaffected_interruption_ms: float
    unaffected_latency_change_ms: float
    unaffected_packet_loss_change: float


class ContinuousBusinessProbe:
    """Observe unaffected business edges during a transaction.

    A background loop follows the configured interval, while the transaction
    executor also requests samples at state-transition boundaries.  The latter
    makes fast in-memory transactions observable without inserting sleeps.
    """

    def __init__(
        self,
        controller: "AgentController",
        stable_state: TaskSubnet,
        unaffected_edge_ids: set[str] | frozenset[str],
        *,
        metric_provider: MetricProvider | None = None,
        interval_ms: float = 10.0,
        healthy_samples_required: int = 2,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        if interval_ms <= 0.0:
            raise ValueError("probe interval must be positive")
        if healthy_samples_required <= 0:
            raise ValueError("healthy_samples_required must be positive")
        self.controller = controller
        self.task_id = stable_state.task.task_id
        self.sessions = {
            session.business_edge_id: session
            for session in stable_state.sessions
            if session.business_edge_id in unaffected_edge_ids
        }
        self.metric_provider = metric_provider
        self.interval_ms = interval_ms
        self.healthy_samples_required = healthy_samples_required
        self.clock = clock
        self.samples: list[BusinessProbeSample] = []
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._sample_index = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.sample("PROBE_STARTED")
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> ContinuousProbeSummary:
        if self._running:
            await self.sample("PROBE_STOPPING")
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return self.summary()

    async def sample(self, stage: str) -> None:
        timestamp = self.clock()
        for edge_id, session in sorted(self.sessions.items()):
            self.samples.append(self._sample_edge(edge_id, session, timestamp, stage))
        self._sample_index += 1
        await asyncio.sleep(0)

    def summary(self) -> ContinuousProbeSummary:
        interrupted_edges = 0
        max_interruption_ms = 0.0
        latency_changes: list[float] = []
        loss_changes: list[float] = []
        for edge_id in self.sessions:
            samples = [item for item in self.samples if item.edge_id == edge_id]
            if not samples:
                continue
            baseline = samples[0]
            latency_changes.extend(item.latency_ms - baseline.latency_ms for item in samples[1:])
            loss_changes.extend(item.packet_loss - baseline.packet_loss for item in samples[1:])
            first_failure = next((idx for idx, item in enumerate(samples) if not item.reachable), None)
            if first_failure is None:
                continue
            interrupted_edges += 1
            recovered_at = samples[-1].probe_timestamp
            healthy_run = 0
            for item in samples[first_failure + 1 :]:
                healthy_run = healthy_run + 1 if item.reachable else 0
                if healthy_run >= self.healthy_samples_required:
                    recovered_at = item.probe_timestamp
                    break
            duration = max(0.0, recovered_at - samples[first_failure].probe_timestamp) * 1000.0
            max_interruption_ms = max(max_interruption_ms, duration)
        return ContinuousProbeSummary(
            samples=tuple(self.samples),
            unaffected_edges_total=len(self.sessions),
            unaffected_edges_interrupted=interrupted_edges,
            unaffected_interruption_ms=max_interruption_ms,
            unaffected_latency_change_ms=(
                sum(latency_changes) / len(latency_changes) if latency_changes else 0.0
            ),
            unaffected_packet_loss_change=(
                sum(loss_changes) / len(loss_changes) if loss_changes else 0.0
            ),
        )

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.interval_ms / 1000.0)
            if self._running:
                await self.sample("PERIODIC")

    def _sample_edge(
        self,
        edge_id: str,
        session: SessionSpec,
        timestamp: float,
        stage: str,
    ) -> BusinessProbeSample:
        rule_reachable = all(
            self.controller.gateways[gateway_id].flow_allowed(
                self.task_id,
                session.source,
                session.target,
            )
            for gateway_id in session.gateway_path
        )
        if self.metric_provider is None:
            return BusinessProbeSample(
                edge_id=edge_id,
                probe_timestamp=timestamp,
                stage=stage,
                reachable=rule_reachable,
                latency_ms=0.0,
                packet_loss=0.0 if rule_reachable else 1.0,
                throughput_mbps=session.data_rate_mbps if rule_reachable else 0.0,
            )
        snapshot = self.metric_provider.snapshot(
            self.task_id,
            session.n_agent_id,
            float(self._sample_index),
        )
        reachable = rule_reachable and snapshot.throughput_mbps > 0.0
        return BusinessProbeSample(
            edge_id=edge_id,
            probe_timestamp=timestamp,
            stage=stage,
            reachable=reachable,
            latency_ms=snapshot.latency_ms,
            packet_loss=snapshot.loss_rate,
            throughput_mbps=snapshot.throughput_mbps if rule_reachable else 0.0,
        )
