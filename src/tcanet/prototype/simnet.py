"""Simulated data plane for ``--sim`` runs (no root, no namespaces).

The model forwards each flow hop by hop along the FT rules the NetAgents
installed — a missing rule, a dead gateway or a down link stops it, just
as in the kernel — and derives goodput, loss and one-way delay from link
capacity (incl. protected load), delay and injected loss.  Faults arrive
on ``sim.fault``; results go out as ``sim.sample`` / ``sim.linkstate``.
"""
from __future__ import annotations

import argparse
import asyncio
import random
from dataclasses import dataclass

from src.tcanet.prototype import config
from src.tcanet.prototype.bus import BusClient
from src.tcanet.scenario_fig1 import ACCESS_CAPACITY_MBPS, load
from src.tcanet.spec import World

ACCESS_DELAY_MS = 1.0
_MAX_HOPS = 8
_OVERHEAD = {"standard": 1.0, "reliable": 1.2, "lightweight": 0.95}


@dataclass
class SimLink:
    link_id: str
    source: str
    target: str
    capacity_mbps: float
    delay_ms: float
    protected_mbps: float
    loss: float = 0.0


@dataclass
class SimFlow:
    dep_id: str
    src_gateway: str
    dst_gateway: str
    rate_mbps: float
    mode: str = "standard"

    @property
    def offered_mbps(self) -> float:
        return self.rate_mbps * _OVERHEAD.get(self.mode, 1.0)


class SimFabricState:
    def __init__(self, world: World) -> None:
        self.links = {
            link.link_id: SimLink(
                link.link_id,
                link.source_gateway,
                link.target_gateway,
                link.capacity_mbps,
                link.delay_ms,
                world.resources[f"link:{link.link_id}"].protected_load_mbps
                if f"link:{link.link_id}" in world.resources
                else 0.0,
            )
            for link in world.graph.links
        }
        self.endpoint_gateway = {
            agent_id: endpoint.gateway_id for agent_id, endpoint in world.endpoints.items()
        }
        self.access = {gateway: ACCESS_CAPACITY_MBPS for gateway in world.graph.gateways}
        self.rules: dict[tuple[str, str], str] = {}  # (gateway, dep) -> next gw | "local"
        self.flows: dict[str, SimFlow] = {}
        self.failed_links: set[str] = set()
        self.failed_gateways: set[str] = set()

    # --- control -----------------------------------------------------
    def set_rule(self, gateway: str, dep_id: str, next_hop: str | None, op: str) -> None:
        if op == "add":
            self.rules[(gateway, dep_id)] = next_hop or "local"
        else:
            self.rules.pop((gateway, dep_id), None)

    def set_gateway(self, gateway: str, up: bool) -> None:
        if up:
            self.failed_gateways.discard(gateway)
        else:
            self.failed_gateways.add(gateway)
            self.rules = {key: hop for key, hop in self.rules.items() if key[0] != gateway}

    def set_link(self, link_id: str, up: bool) -> None:
        if up:
            self.failed_links.discard(link_id)
        else:
            self.failed_links.add(link_id)

    def degrade(self, link_id: str, loss: float) -> None:
        self.links[link_id].loss = loss

    def start_flow(self, dep_id: str, src_agent: str, dst_agent: str, rate: float, mode: str) -> None:
        self.flows[dep_id] = SimFlow(
            dep_id, self.endpoint_gateway[src_agent], self.endpoint_gateway[dst_agent], rate, mode
        )

    def stop_flow(self, dep_id: str) -> None:
        self.flows.pop(dep_id, None)

    # --- model -------------------------------------------------------
    def link_up(self, link_id: str) -> bool:
        link = self.links[link_id]
        return (
            link_id not in self.failed_links
            and link.source not in self.failed_gateways
            and link.target not in self.failed_gateways
        )

    def route(self, dep_id: str) -> list[str] | None:
        """Link ids the flow traverses following installed rules, or None."""
        flow = self.flows[dep_id]
        gateway, used = flow.src_gateway, []
        for _ in range(_MAX_HOPS):
            if gateway in self.failed_gateways:
                return None
            hop = self.rules.get((gateway, dep_id))
            if hop is None:
                return None
            if hop == "local":
                return used if gateway == flow.dst_gateway else None
            link = next(
                (l for l in self.links.values()
                 if l.source == gateway and l.target == hop and self.link_up(l.link_id)),
                None,
            )
            if link is None:
                return None
            used.append(link.link_id)
            gateway = hop
        return None

    def evaluate(self) -> tuple[dict[str, dict], dict[str, dict]]:
        routes = {dep: self.route(dep) for dep in self.flows}
        load = {link_id: link.protected_mbps for link_id, link in self.links.items()}
        access_load: dict[tuple[str, str], float] = {}
        for dep, links in routes.items():
            if links is None:
                continue
            flow = self.flows[dep]
            for link_id in links:
                load[link_id] += flow.offered_mbps
            access_load[("ul", flow.src_gateway)] = access_load.get(("ul", flow.src_gateway), 0.0) + flow.offered_mbps
            access_load[("dl", flow.dst_gateway)] = access_load.get(("dl", flow.dst_gateway), 0.0) + flow.offered_mbps
        samples: dict[str, dict] = {}
        for dep, links in routes.items():
            flow = self.flows[dep]
            if links is None:
                samples[dep] = {"rx_mbps": 0.0, "loss": None, "owd_ms": None}
                continue
            share = 1.0
            delay = 2 * ACCESS_DELAY_MS
            survive = 1.0
            for link_id in links:
                link = self.links[link_id]
                share = min(share, link.capacity_mbps / max(load[link_id], 1e-9))
                delay += link.delay_ms + (5.0 if load[link_id] > 0.9 * link.capacity_mbps else 0.0)
                link_loss = link.loss * (0.5 if flow.mode == "reliable" else 1.0)
                survive *= 1.0 - link_loss
            for key in (("ul", flow.src_gateway), ("dl", flow.dst_gateway)):
                share = min(share, self.access[key[1]] / max(access_load[key], 1e-9))
            loss = 1.0 - survive * min(1.0, share)
            samples[dep] = {
                "rx_mbps": flow.rate_mbps * (1.0 - loss),
                "loss": loss,
                "owd_ms": delay,
            }
        states = {
            link_id: {
                "up": self.link_up(link_id),
                "tx_mbps": min(load[link_id], link.capacity_mbps) if self.link_up(link_id) else 0.0,
            }
            for link_id, link in self.links.items()
        }
        return samples, states


class SimNetworkProcess:
    def __init__(self, bus: BusClient, state: SimFabricState, *, seed: int = 7) -> None:
        self.bus = bus
        self.state = state
        self.rng = random.Random(seed)

    def handle(self, msg: dict) -> None:
        topic, p = msg["topic"], msg["payload"]
        state = self.state
        if topic == "sim.rule":
            state.set_rule(p["gateway"], p["dep_id"], p.get("next_hop"), p["op"])
        elif topic == "sim.flow":
            if p["op"] == "start":
                state.start_flow(p["dep_id"], p["src_agent"], p["dst_agent"], p["rate_mbps"], p.get("mode", "standard"))
            elif p["op"] == "update" and p["dep_id"] in state.flows:
                flow = state.flows[p["dep_id"]]
                flow.rate_mbps = float(p.get("rate_mbps", flow.rate_mbps))
                flow.mode = p.get("mode", flow.mode)
            elif p["op"] == "stop":
                state.stop_flow(p["dep_id"])
        elif topic == "sim.access":
            state.access[p["gateway"]] = float(p["capacity_mbps"])
        elif topic == "sim.fault":
            kind = p["kind"]
            if kind == "gateway":
                state.set_gateway(p["gateway"], bool(p["up"]))
            elif kind == "link":
                state.set_link(p["link_id"], bool(p["up"]))
            elif kind == "degrade":
                state.degrade(p["link_id"], float(p["loss"]))
            elif kind == "reset":
                state.failed_links.clear()
                state.failed_gateways.clear()
                for link in state.links.values():
                    link.loss = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        await self.bus.subscribe("sim.rule", "sim.flow", "sim.access", "sim.fault")
        ticker = asyncio.create_task(self._tick(stop))
        try:
            while not stop.is_set():
                try:
                    msg = await self.bus.recv(timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                if msg is None:
                    break
                self.handle(msg)
        finally:
            ticker.cancel()

    async def _tick(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(config.sample_period_s())
            samples, states = self.state.evaluate()
            for dep, sample in samples.items():
                noisy = dict(sample)
                if noisy["rx_mbps"] > 0:
                    noisy["rx_mbps"] = max(0.0, noisy["rx_mbps"] * self.rng.gauss(1.0, 0.02))
                    noisy["owd_ms"] = noisy["owd_ms"] + abs(self.rng.gauss(0.0, 0.4))
                await self.bus.publish("sim.sample", {"dep_id": dep, **noisy})
            await self.bus.publish("sim.linkstate", {"links": states})


async def _main(scenario: str) -> None:
    world, _task = load(scenario)
    bus = await BusClient.connect(config.bus_path(), "simnet")
    stop = asyncio.Event()
    await SimNetworkProcess(bus, SimFabricState(world)).run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet simulated data plane")
    parser.add_argument("--scenario", default="paper_fig1")
    asyncio.run(_main(parser.parse_args().scenario))


if __name__ == "__main__":
    main()
