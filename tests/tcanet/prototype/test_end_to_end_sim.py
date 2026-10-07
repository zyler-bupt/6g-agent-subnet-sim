"""All demo acts end to end on the simulated data plane, in one process."""
from __future__ import annotations

import asyncio
import io
import unittest

from rich.console import Console

from src.tcanet.prototype import ctl
from src.tcanet.prototype.addressing import build_plan
from src.tcanet.prototype.agents.__main__ import build_agent
from src.tcanet.prototype.controller import ControllerApp
from src.tcanet.prototype.runtime import agent_specs
from src.tcanet.prototype.simnet import SimFabricState, SimNetworkProcess
from src.tcanet.scenario_fig1 import load
from tests.tcanet.prototype.harness import BusHarness, wait_for


class InProcessRuntime:
    """``ctl.Runtime`` over asyncio tasks instead of OS processes."""

    mode = "sim"

    def __init__(self, harness: BusHarness, world, plan) -> None:
        self.harness = harness
        self.specs = {spec.agent_id: spec for spec in agent_specs(world, plan)}
        self.running: dict[str, tuple[asyncio.Event, asyncio.Task]] = {}

    def revive(self, agent_id: str) -> bool:
        if agent_id in self.running or agent_id not in self.specs:
            return False
        spec = self.specs[agent_id]
        agent = build_agent(spec.role, spec.agent_id, spec.gateway, scenario="paper_fig1",
                            sim=True, bus_path=self.harness.sock)
        agent.console = Console(file=io.StringIO())
        stop = asyncio.Event()
        self.running[agent_id] = (stop, asyncio.create_task(agent.run(stop)))
        return True

    def kill(self, agent_id: str) -> bool:
        entry = self.running.pop(agent_id, None)
        if entry is None:
            return False
        entry[1].cancel()  # abrupt: no goodbye, heartbeats just stop
        return True

    async def kernel(self, cmds) -> None:
        raise AssertionError("sim runtime must not touch the kernel")

    async def shutdown(self) -> None:
        for stop, task in self.running.values():
            stop.set()
            task.cancel()
        await asyncio.gather(*(task for _s, task in self.running.values()), return_exceptions=True)


def _commit(version: int):
    return lambda m: m["topic"] == "ctrl.subnet" and m["payload"]["version"] == version


class EndToEndSimTests(unittest.TestCase):
    def test_all_acts(self) -> None:
        asyncio.run(self._acts())

    async def _acts(self) -> None:
        async with BusHarness() as h:
            world, _task = load("paper_fig1")
            plan = build_plan(world)
            stop = asyncio.Event()
            simnet = SimNetworkProcess(await h.client("simnet"), SimFabricState(world))
            controller = ControllerApp(await h.client("controller"), console=Console(file=io.StringIO()))
            background = [asyncio.create_task(simnet.run(stop)), asyncio.create_task(controller.run(stop))]
            rt = InProcessRuntime(h, world, plan)
            for agent_id in rt.specs:
                rt.revive(agent_id)
            watch = await h.client("watch", "ctrl.", "ack")
            op = await h.client("tcanetctl")
            try:
                await asyncio.sleep(0.6)
                # Act 1: formation
                await ctl.submit(op, "paper_fig1")
                v1 = await wait_for(watch, _commit(1), timeout=8)
                self.assertEqual(v1["payload"]["paths"]["e1"], ["G1", "G2", "G4"])

                # Act 2: demand 15 -> 25 Mbps on e1 (task update)
                await ctl.demand(op, "e1", 25.0)
                v2 = await wait_for(watch, _commit(2), timeout=8)
                steps = [item["step"] for item in controller.history]
                self.assertIn("select", steps)

                # Act 3: transit gateway G2 fails
                mark = len(controller.history)
                await ctl.set_gateway(op, rt, plan, "G2", up=False)
                v3 = await wait_for(watch, _commit(3), timeout=10)
                self.assertEqual(v3["payload"]["paths"]["e1"], ["G1", "G3", "G4"])
                self.assertEqual(v3["payload"]["paths"]["e3"], v2["payload"]["paths"]["e3"])
                scope = next(i for i in controller.history[mark:] if i["step"] == "scope")
                self.assertIn("retained unchanged: {e3}", scope["lines"][-1])

                # Act 4: hidden L3 loss -> Assess fails -> Rollback -> L6
                await ctl.reset(op, rt, world, plan)
                await wait_for(watch, _commit(0), timeout=5)
                await asyncio.sleep(0.6)
                await ctl.submit(op, "paper_fig1")
                await wait_for(watch, _commit(1), timeout=8)
                await ctl.degrade(op, rt, world, plan, "L3", 10.0)
                mark = len(controller.history)
                await ctl.set_gateway(op, rt, plan, "G2", up=False)
                v2b = await wait_for(watch, _commit(2), timeout=12)
                self.assertEqual(v2b["payload"]["paths"]["e1"], ["G1", "G4"])
                steps = [i["step"] for i in controller.history[mark:]]
                self.assertIn("rollback", steps)
                assess_fail = next(i for i in controller.history[mark:]
                                   if i["step"] == "assess" and i["level"] == "fail")
                self.assertIn("loss_hard:e1", assess_fail["title"])

                # Act 5: PhyAgent at G4 dies -> rebind e3 only
                await ctl.kill(op, rt, "physical-G4")
                v3b = await wait_for(watch, _commit(3), timeout=10)
                self.assertNotEqual(v3b["payload"]["bindings"]["e3"][2], "physical-G4")
                self.assertEqual(v3b["payload"]["paths"], v2b["payload"]["paths"])
            finally:
                stop.set()
                await rt.shutdown()
                for task in background:
                    task.cancel()
                await asyncio.gather(*background, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
