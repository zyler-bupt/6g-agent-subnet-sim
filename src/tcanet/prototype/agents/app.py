"""AppAgent: task endpoint workload behind a replaceable ``AppAdapter``.

The synthetic adapter streams one measured UDP flow per outgoing task
dependency at the requested demand ``r_{m,e}``.  A real application agent
implements the same ``AppAdapter`` protocol and sends its own traffic from
the endpoint's namespace; nothing else changes.
"""
from __future__ import annotations

import asyncio
from typing import Protocol

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import AddressPlan, flow_port
from src.tcanet.prototype.agents.base import AgentBase
from src.tcanet.spec import Dependency, TaskSpecification


class AppAdapter(Protocol):
    async def start(self, agent: "AppAgent") -> None: ...

    async def activate(self) -> None: ...

    async def deactivate(self) -> None: ...

    async def set_demand(self, dep_id: str, mbps: float) -> None: ...

    async def set_rate(self, dep_id: str, mbps: float) -> None: ...

    async def set_mode(self, dep_id: str, mode: str) -> None: ...

    def demand(self) -> dict[str, dict]: ...

    async def stop(self) -> None: ...


class SyntheticAppAdapter:
    """One measured UDP stream per outgoing dependency."""

    def __init__(self) -> None:
        self.agent: AppAgent | None = None
        self.out: dict[str, dict] = {}
        self._sink_stop = asyncio.Event()
        self._send_stop: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []
        self._senders: dict[str, object] = {}

    async def start(self, agent: "AppAgent") -> None:
        self.agent = agent
        for dep in agent.outgoing:
            self.out[dep.dep_id] = {"demand": dep.demand_mbps, "rate": dep.demand_mbps, "mode": "standard"}
        for dep in agent.incoming:
            sink = agent.fabric.sink(
                dep.dep_id, flow_port(agent.task, dep.dep_id),
                lambda sample, dep_id=dep.dep_id: agent.report_sample(dep_id, sample),
                config.sample_period_s(),
            )
            self._tasks.append(asyncio.create_task(sink.run(self._sink_stop)))

    async def activate(self) -> None:
        if self._send_stop is not None:
            return
        self._send_stop = asyncio.Event()
        agent = self.agent
        for dep in agent.outgoing:
            state = self.out[dep.dep_id]
            sender = agent.fabric.sender(
                dep.dep_id, dep.source, dep.target,
                agent.plan.endpoints[dep.target].endpoint_ip,
                flow_port(agent.task, dep.dep_id), state["rate"], state["mode"],
            )
            self._senders[dep.dep_id] = sender
            self._tasks.append(asyncio.create_task(sender.run(self._send_stop)))

    async def deactivate(self) -> None:
        if self._send_stop is not None:
            self._send_stop.set()
            self._send_stop = None
        self._senders.clear()

    async def set_demand(self, dep_id: str, mbps: float) -> None:
        self.out[dep_id]["demand"] = mbps
        await self.set_rate(dep_id, mbps)

    async def set_rate(self, dep_id: str, mbps: float) -> None:
        self.out[dep_id]["rate"] = mbps
        if dep_id in self._senders:
            self._senders[dep_id].set_rate(mbps)

    async def set_mode(self, dep_id: str, mode: str) -> None:
        self.out[dep_id]["mode"] = mode
        if dep_id in self._senders:
            self._senders[dep_id].set_mode(mode)

    def demand(self) -> dict[str, dict]:
        return {dep_id: dict(state) for dep_id, state in self.out.items()}

    async def stop(self) -> None:
        await self.deactivate()
        self._sink_stop.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class AppAgent(AgentBase):
    role = "application"

    def __init__(self, agent_id: str, gateway: str, *, endpoint: str, task: TaskSpecification,
                 plan: AddressPlan, adapter: AppAdapter | None = None, **kwargs) -> None:
        super().__init__(agent_id, gateway, **kwargs)
        self.endpoint = endpoint
        self.task = task
        self.plan = plan
        self.adapter = adapter or SyntheticAppAdapter()
        self.outgoing: tuple[Dependency, ...] = tuple(
            dep for dep in task.dag.dependencies if dep.source == endpoint
        )
        self.incoming: tuple[Dependency, ...] = tuple(
            dep for dep in task.dag.dependencies if dep.target == endpoint
        )
        self.active = False

    def topics(self) -> tuple[str, ...]:
        return super().topics() + ("ctl.demand", "ctrl.subnet", "ctrl.staged", "cmd.session")

    async def started(self) -> None:
        await self.adapter.start(self)
        await super().started()

    async def stopped(self) -> None:
        await self.adapter.stop()

    def background(self, stop: asyncio.Event) -> tuple:
        return (self._report_demand(stop),)

    async def _report_demand(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._publish_demand()
            await asyncio.sleep(config.radio_period_s())

    async def _publish_demand(self) -> None:
        for dep_id, state in self.adapter.demand().items():
            await self.bus.publish("report.app", {
                "dep_id": dep_id, "demand_mbps": state["demand"], "rate_mbps": state["rate"],
                "mode": state["mode"], "active": self.active, "reporter": self.agent_id,
            })

    async def report_sample(self, dep_id: str, sample: dict) -> None:
        await self.bus.publish("report.flow", {"dep_id": dep_id, **sample,
                                               "source": self.fabric.source,
                                               "reporter": self.agent_id})

    async def on_message(self, msg: dict) -> None:
        topic, payload = msg["topic"], msg["payload"]
        outgoing = {dep.dep_id for dep in self.outgoing}
        if topic == "ctl.demand" and payload["dep_id"] in outgoing:
            old = self.adapter.demand()[payload["dep_id"]]["demand"]
            await self.adapter.set_demand(payload["dep_id"], float(payload["mbps"]))
            await self.log(f"task update: {payload['dep_id']} demand {old:g} → {float(payload['mbps']):g} Mbps", "warn")
            await self._publish_demand()
        elif topic in ("ctrl.staged", "ctrl.subnet"):
            if payload.get("version", 0) >= 1 and not self.active and self.outgoing:
                self.active = True
                await self.adapter.activate()
                await self.log(f"subnet v{payload['version']} {'staged' if topic == 'ctrl.staged' else 'active'}: streaming "
                               + ", ".join(dep.dep_id for dep in self.outgoing))
            elif topic == "ctrl.subnet" and payload.get("version", 0) == 0 and self.active:
                self.active = False
                await self.adapter.deactivate()
                await self.log("subnet withdrawn: streaming stopped")
        elif topic == "cmd.session" and payload["app_agent"] == self.agent_id:
            await self.adapter.set_mode(payload["dep_id"], payload["mode"])

    async def handle(self, payload: dict) -> tuple[bool, str]:
        if payload["kind"] == "layer_operation" and payload["value"] == "ADJUST_RATE":
            rate = float(payload["params"]["rate_mbps"])
            await self.adapter.set_rate(payload["target"], rate)
            await self.log(f"source rate {payload['target']} → {rate:g} Mbps")
            return True, f"rate {rate:g}"
        return await super().handle(payload)
