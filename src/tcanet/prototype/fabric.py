"""How agents touch the data plane: kernel (netns) or simulator (sim).

Agents call the same methods in both modes:

* ``apply_rule(params, op)`` — NetAgent installs/withdraws one FT entry;
* ``read_links(ifnames)`` — NetAgent reads ``{link_id: (up, tx_mbps)}``;
* ``set_access(gateway, mbps)`` — PhyAgent shapes the gateway's access links;
* ``sender(...)`` / ``sink(...)`` — AppAgent task traffic.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from src.tcanet.prototype.addressing import AddressPlan, rule_commands
from src.tcanet.prototype.bus import BusClient
from src.tcanet.prototype.flow import FlowSender, FlowSink
from src.tcanet.prototype.topology import access_qdiscs, run_commands


class NetnsFabric:
    """Kernel-backed fabric; the agent process already runs in its netns."""

    source = "MEASURED"

    def __init__(self, plan: AddressPlan, *, runner=subprocess.run, sysfs: Path = Path("/sys/class/net")) -> None:
        self.plan = plan
        self.runner = runner
        self.sysfs = sysfs
        self._last: dict[str, tuple[float, int]] = {}

    async def apply_rule(self, params: dict, op: str) -> tuple[bool, str]:
        cmds = rule_commands(params, op)
        try:
            lines = await asyncio.to_thread(run_commands, cmds, runner=self.runner)
        except RuntimeError as exc:
            return False, str(exc)
        return True, "; ".join(lines)

    async def read_links(self, ifnames: dict[str, str]) -> dict[str, tuple[bool, float]]:
        now = time.monotonic()
        result: dict[str, tuple[bool, float]] = {}
        for link_id, ifname in ifnames.items():
            base = self.sysfs / ifname
            try:
                up = (base / "operstate").read_text().strip() == "up"
                tx_bytes = int((base / "statistics" / "tx_bytes").read_text())
            except (FileNotFoundError, ValueError, OSError):
                result[link_id] = (False, 0.0)
                continue
            last = self._last.get(link_id)
            self._last[link_id] = (now, tx_bytes)
            rate = 0.0
            if last is not None and now > last[0]:
                rate = max(0, tx_bytes - last[1]) * 8 / (now - last[0]) / 1e6
            result[link_id] = (up, rate)
        return result

    async def set_access(self, gateway: str, mbps: float) -> None:
        cmds = [
            cmd
            for endpoint in self.plan.endpoints_at(gateway)
            for cmd in access_qdiscs(self.plan, endpoint.agent_id, mbps)
        ]
        await asyncio.to_thread(run_commands, cmds, runner=self.runner)

    def sender(self, dep_id: str, src_agent: str, dst_agent: str, dst_ip: str, port: int,
               rate_mbps: float, mode: str) -> FlowSender:
        return FlowSender(dst_ip, port, rate_mbps, mode=mode)

    def sink(self, dep_id: str, port: int, on_sample, period_s: float) -> FlowSink:
        return FlowSink(port, on_sample, period_s=period_s)


class _SimSender:
    def __init__(self, bus: BusClient, dep_id: str, src_agent: str, dst_agent: str,
                 rate_mbps: float, mode: str) -> None:
        self.bus, self.dep_id = bus, dep_id
        self.src_agent, self.dst_agent = src_agent, dst_agent
        self.rate_mbps, self.mode = rate_mbps, mode
        self._dirty = False

    def set_rate(self, rate_mbps: float) -> None:
        self.rate_mbps = float(rate_mbps)
        self._dirty = True

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._dirty = True

    async def run(self, stop: asyncio.Event) -> None:
        await self.bus.publish("sim.flow", {
            "op": "start", "dep_id": self.dep_id, "src_agent": self.src_agent,
            "dst_agent": self.dst_agent, "rate_mbps": self.rate_mbps, "mode": self.mode,
        })
        try:
            while not stop.is_set():
                if self._dirty:
                    self._dirty = False
                    await self.bus.publish("sim.flow", {
                        "op": "update", "dep_id": self.dep_id,
                        "rate_mbps": self.rate_mbps, "mode": self.mode,
                    })
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.05)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.bus.publish("sim.flow", {"op": "stop", "dep_id": self.dep_id})


class _SimSink:
    def __init__(self, fabric: "SimFabric", dep_id: str, on_sample) -> None:
        self.fabric, self.dep_id, self.on_sample = fabric, dep_id, on_sample

    async def run(self, stop: asyncio.Event) -> None:
        self.fabric._sinks[self.dep_id] = self.on_sample
        try:
            await stop.wait()
        finally:
            self.fabric._sinks.pop(self.dep_id, None)


class SimFabric:
    """Simulator-backed fabric; talks to ``SimNetworkProcess`` over the bus."""

    source = "SIM"

    def __init__(self, bus: BusClient) -> None:
        self.bus = bus
        self._links: dict[str, dict] = {}
        self._sinks: dict[str, object] = {}

    async def feed(self, msg: dict) -> None:
        if msg["topic"] == "sim.linkstate":
            self._links = msg["payload"]["links"]
        elif msg["topic"] == "sim.sample":
            callback = self._sinks.get(msg["payload"]["dep_id"])
            if callback is not None:
                sample = {k: v for k, v in msg["payload"].items() if k != "dep_id"}
                result = callback(sample)
                if asyncio.iscoroutine(result):
                    await result

    async def apply_rule(self, params: dict, op: str) -> tuple[bool, str]:
        next_hop = params["next_hop_gateway"] if params["mode"] == "forward_to_gateway" else None
        await self.bus.publish("sim.rule", {
            "gateway": params["gateway"], "dep_id": params["dep_id"],
            "next_hop": next_hop, "op": op,
        })
        return True, "; ".join(f"[SIM] {cmd.text()}" for cmd in rule_commands(params, op))

    async def read_links(self, ifnames: dict[str, str]) -> dict[str, tuple[bool, float]]:
        return {
            link_id: (bool(self._links[link_id]["up"]), float(self._links[link_id]["tx_mbps"]))
            for link_id in ifnames
            if link_id in self._links
        }

    async def set_access(self, gateway: str, mbps: float) -> None:
        await self.bus.publish("sim.access", {"gateway": gateway, "capacity_mbps": mbps})

    def sender(self, dep_id: str, src_agent: str, dst_agent: str, dst_ip: str, port: int,
               rate_mbps: float, mode: str) -> _SimSender:
        return _SimSender(self.bus, dep_id, src_agent, dst_agent, rate_mbps, mode)

    def sink(self, dep_id: str, port: int, on_sample, period_s: float) -> _SimSink:
        return _SimSink(self, dep_id, on_sample)
