from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import build_plan
from src.tcanet.prototype.bus import BusClient
from src.tcanet.prototype.root import down, process_plan, up
from src.tcanet.prototype.runtime import ProcessTable, agent_argv, agent_specs
from src.tcanet.scenario_fig1 import load


class ProcessPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world, _task = load("paper_fig1")
        self.plan = build_plan(self.world)

    def test_sixteen_agents_in_their_namespaces(self) -> None:
        specs = agent_specs(self.world, self.plan)
        self.assertEqual(len(specs), 16)
        app = next(spec for spec in specs if spec.agent_id == "app-a3")
        self.assertEqual((app.gateway, app.ns), ("G4", "tc-a3"))
        argv = agent_argv(app, sim=False, scenario="paper_fig1", python="py")
        self.assertEqual(argv[:5], ["ip", "netns", "exec", "tc-a3", "py"])
        self.assertNotIn("ip", agent_argv(app, sim=True, scenario="paper_fig1", python="py")[:1])

    def test_netns_plan_has_protected_traffic_on_l1_only(self) -> None:
        names = [name for name, _argv in process_plan("netns", self.world, self.plan, "paper_fig1", "py")]
        self.assertEqual(names[0], "bus")
        self.assertIn("bg-L1", names)
        self.assertNotIn("bg-L2", names)
        self.assertNotIn("simnet", names)
        sim_names = [name for name, _ in process_plan("sim", self.world, self.plan, "paper_fig1", "py")]
        self.assertIn("simnet", sim_names)
        self.assertEqual(len(sim_names), 18)


class SimUpDownTests(unittest.TestCase):
    """Real subprocesses: ``root up --mode sim`` brings all agents online."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))
        self.env = mock.patch.dict(os.environ, {"TCANET_RUN_DIR": str(self.dir),
                                                 "TCANET_TIME_SCALE": "0.5"})
        self.env.start()

    def tearDown(self) -> None:
        down()
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_all_agents_say_hello(self) -> None:
        up("sim")
        self.assertEqual(config.read_mode(), "sim")

        async def collect():
            client = await BusClient.connect(config.bus_path(), "probe", ("heartbeat",))
            seen: set[str] = set()
            deadline = asyncio.get_running_loop().time() + 20
            while len(seen) < 16 and asyncio.get_running_loop().time() < deadline:
                try:
                    msg = await client.recv(timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                seen.add(msg["payload"]["agent_id"])
            await client.close()
            return seen

        seen = asyncio.run(collect())
        self.assertEqual(len(seen), 16, sorted(seen))
        self.assertTrue(ProcessTable(config.pids_dir()).alive("simnet"))


if __name__ == "__main__":
    unittest.main()
