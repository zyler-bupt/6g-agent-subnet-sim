"""Assessment from real flow samples (paper Alg. 1, line 16).

After Apply, wait one assessment window, then summarise every affected
dependency's samples (median goodput, 90th-percentile one-way delay, mean
loss).  A dependency without packets is reported as zero goodput, infinite
delay and total loss, so the hard-constraint check rejects it.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from typing import Awaitable, Callable

from src.tcanet.executor import StagedDecision
from src.tcanet.prototype import config
from src.tcanet.prototype.aggregator import FlowBook
from src.tcanet.spec import World
from src.tcanet.verify import DepObservation


class BusMeasurement:
    def __init__(
        self,
        flowbook: FlowBook,
        *,
        window_s: float | None = None,
        settle_s: float | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.flowbook = flowbook
        self.window_s = window_s
        self.settle_s = settle_s
        self.clock = clock
        self.sleep = sleep

    @property
    def window_ms(self) -> float:
        return (self.window_s or config.assess_window_s()) * 1000.0

    async def __call__(self, staged: StagedDecision, world: World, dep_ids: tuple[str, ...]) -> tuple[DepObservation, ...]:
        window_s = self.window_s or config.assess_window_s()
        settle_s = self.settle_s if self.settle_s is not None else config.assess_settle_s()
        started = self.clock()
        await self.sleep(window_s)
        observations = []
        for dep_id in sorted(dep_ids):
            samples = self.flowbook.since(dep_id, started + settle_s)
            if not samples:
                continue
            rates = [sample["rx_mbps"] for _ts, sample in samples]
            delays = sorted(s["owd_ms"] for _ts, s in samples if s.get("owd_ms") is not None)
            losses = [s["loss"] for _ts, s in samples if s.get("loss") is not None]
            observations.append(DepObservation(
                dep_id=dep_id,
                observed_at_ms=min(window_s * 1000.0, (samples[-1][0] - started) * 1000.0),
                throughput_mbps=statistics.median(rates),
                delay_ms=delays[int(0.9 * (len(delays) - 1))] if delays else float("inf"),
                loss_rate=statistics.mean(losses) if losses else 1.0,
            ))
        return tuple(observations)
