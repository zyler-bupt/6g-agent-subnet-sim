"""PhyAgent: simulated access-link radio state that really shapes the data plane."""
from __future__ import annotations

import asyncio
import random

from src.tcanet.prototype import config
from src.tcanet.prototype.agents.base import AgentBase
from src.tcanet.scenario_fig1 import ACCESS_CAPACITY_MBPS

SNR_NOMINAL_DB = 20.0


def access_capacity(snr_db: float, boost_mbps: float) -> float:
    """Access capacity from SNR (SIM model): nominal 60 Mbps at 20 dB."""
    return round(ACCESS_CAPACITY_MBPS * min(1.0, snr_db / SNR_NOMINAL_DB) + boost_mbps, 1)


class PhyAgent(AgentBase):
    role = "physical"

    def __init__(self, agent_id: str, gateway: str, *, seed: int = 0, **kwargs) -> None:
        super().__init__(agent_id, gateway, **kwargs)
        self.rng = random.Random(seed or hash(agent_id) & 0xFFFF)
        self.snr_db = SNR_NOMINAL_DB
        self.boost_mbps = 0.0

    def background(self, stop: asyncio.Event) -> tuple:
        return (self._radio(stop),)

    def step(self) -> float:
        drift = self.rng.gauss(0.0, 0.6) + 0.1 * (SNR_NOMINAL_DB - self.snr_db)
        self.snr_db = max(12.0, min(26.0, self.snr_db + drift))
        return access_capacity(self.snr_db, self.boost_mbps)

    async def _radio(self, stop: asyncio.Event) -> None:
        applied: float | None = None
        while not stop.is_set():
            capacity = self.step()
            if applied is None or abs(capacity - applied) >= 2.0:
                await self.fabric.set_access(self.gateway, capacity)
                applied = capacity
            await self.bus.publish("report.access", {
                "gateway": self.gateway, "snr_db": round(self.snr_db, 2),
                "capacity_mbps": capacity, "source": "SIM", "reporter": self.agent_id,
            })
            await asyncio.sleep(config.radio_period_s())

    async def handle(self, payload: dict) -> tuple[bool, str]:
        if payload["kind"] == "layer_operation" and payload["value"] == "BOOST_ACCESS":
            delta = float(payload["params"]["boost_mbps"])
            self.boost_mbps += delta
            await self.log(f"access boost {delta:+g} Mbps → {access_capacity(self.snr_db, self.boost_mbps)} Mbps")
            return True, f"boost {self.boost_mbps:g}"
        return await super().handle(payload)
