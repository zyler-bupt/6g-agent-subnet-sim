"""NetAgent: link observation and task forwarding-state execution."""
from __future__ import annotations

import asyncio

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import AddressPlan
from src.tcanet.prototype.agents.base import AgentBase
from src.tcanet.spec import World


class NetAgent(AgentBase):
    role = "network"

    def __init__(self, agent_id: str, gateway: str, *, world: World, plan: AddressPlan, **kwargs) -> None:
        super().__init__(agent_id, gateway, **kwargs)
        self.world = world
        self.plan = plan
        self.links = {link.link_id: link for link in plan.links_at(gateway)}

    def background(self, stop: asyncio.Event) -> tuple:
        return (self._probe(stop),)

    async def _probe(self, stop: asyncio.Event) -> None:
        last_up: dict[str, bool] = {}
        while not stop.is_set():
            states = await self.fabric.read_links(
                {link_id: link.ifname for link_id, link in self.links.items()}
            )
            for link_id, (up, tx_mbps) in sorted(states.items()):
                egress = self.links[link_id].source_gw == self.gateway
                capacity = self.world.graph.link(link_id).capacity_mbps
                await self.bus.publish("report.link", {
                    "link_id": link_id, "up": up, "reporter": self.agent_id,
                    "tx_mbps": tx_mbps if egress else None,
                    "utilization": tx_mbps / capacity if egress else None,
                    "source": self.fabric.source,
                })
                if link_id in last_up and last_up[link_id] != up:
                    await self.log(f"{link_id} {'carrier restored' if up else 'carrier LOST'}",
                                   "ok" if up else "fail")
                last_up[link_id] = up
            await asyncio.sleep(config.sample_period_s())

    async def handle(self, payload: dict) -> tuple[bool, str]:
        if payload["kind"] in ("install_rule", "remove_rule"):
            op = "add" if payload["kind"] == "install_rule" else "del"
            params = payload["params"]
            ok, detail = await self.fabric.apply_rule(params, op)
            hop = "local delivery" if params["mode"] == "local_delivery" else f"→ {params['next_hop_gateway']}"
            sign = "+" if op == "add" else "−"
            await self.log(f"FT {sign} {params['dep_id']} {hop} (table {params['table']})"
                           + ("" if ok else f"  FAILED: {detail}"), "info" if ok else "fail")
            return ok, detail
        return await super().handle(payload)
