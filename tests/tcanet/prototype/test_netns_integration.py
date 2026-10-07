"""Real kernel data plane: FT rules forward a measured flow (Linux + root only).

Run on the Ubuntu demo host:
    sudo -E .venv/bin/python -m pytest -q tests/tcanet/prototype/test_netns_integration.py
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from src.tcanet.prototype.addressing import Cmd, build_plan, gateway_ns, in_ns, rule_commands, rule_params
from src.tcanet.prototype.topology import build_commands, run_commands, teardown_commands
from src.tcanet.scenario import run_formation
from src.tcanet.scenario_fig1 import ACCESS_CAPACITY_MBPS, load

REPO_ROOT = Path(__file__).resolve().parents[3]
_SINK = (
    "import asyncio,sys\n"
    "from src.tcanet.prototype.flow import FlowSink\n"
    "async def main():\n"
    "    stop=asyncio.Event()\n"
    "    def show(s): print(round(s['rx_mbps'],2), flush=True)\n"
    "    task=asyncio.create_task(FlowSink(47000, show, period_s=0.5).run(stop))\n"
    "    await asyncio.sleep(float(sys.argv[1])); stop.set(); await task\n"
    "asyncio.run(main())\n"
)


def _runnable() -> bool:
    return (platform.system() == "Linux" and os.geteuid() == 0
            and shutil.which("ip") is not None and shutil.which("tc") is not None)


@unittest.skipUnless(_runnable(), "needs Linux, root, iproute2")
class NetnsDataPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        existing = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout
        if "tc-g1" in existing:
            self.skipTest("a prototype topology is running; run `tcanet-demo down` first")
        self.world, self.task = load("paper_fig1")
        self.plan = build_plan(self.world)
        run_commands(build_commands(self.world, self.plan, ACCESS_CAPACITY_MBPS))

    def tearDown(self) -> None:
        run_commands(teardown_commands(self.world, self.plan))

    def _install_e1(self) -> None:
        subnet = asyncio.run(run_formation(self.task, self.world)).subnet
        for rule_id, entry in subnet.forwarding.items():
            if entry.dep_id != "e1":
                continue
            params = rule_params(entry, self.task, self.plan)
            run_commands([in_ns(gateway_ns(entry.gateway_id), cmd) for cmd in rule_commands(params, "add")])

    def _measure(self, seconds: float = 3.0) -> list[float]:
        sink = subprocess.Popen(["ip", "netns", "exec", "tc-a3", sys.executable, "-c", _SINK, str(seconds)],
                                cwd=REPO_ROOT, stdout=subprocess.PIPE, text=True)
        time.sleep(0.3)
        sender = subprocess.Popen(["ip", "netns", "exec", "tc-a1", sys.executable, "-m",
                                   "src.tcanet.prototype.background", "send", "--dst", "10.1.3.2",
                                   "--port", "47000", "--mbps", "10"], cwd=REPO_ROOT)
        out, _ = sink.communicate(timeout=seconds + 5)
        sender.terminate()
        sender.wait(timeout=5)
        return [float(line) for line in out.split()]

    def test_flow_needs_ft_rules(self) -> None:
        self.assertLess(max(self._measure(2.0) or [0.0]), 0.5)  # no FT -> no forwarding
        self._install_e1()
        rates = self._measure(3.0)
        self.assertGreater(max(rates), 7.0, rates)
        rules = subprocess.run(["ip", "-n", "tc-g1", "rule", "show"], capture_output=True, text=True).stdout
        self.assertIn("lookup 100", rules)

    def test_gateway_down_stops_the_flow(self) -> None:
        self._install_e1()
        run_commands([in_ns("tc-g2", Cmd(("ip", "link", "set", "l1", "down")))])
        self.assertLess(max(self._measure(2.0) or [0.0]), 0.5)


if __name__ == "__main__":
    unittest.main()
