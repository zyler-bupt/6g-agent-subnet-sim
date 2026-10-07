"""Agent process entry point.

``python -m src.tcanet.prototype.agents --role network --agent-id network-G1 --gateway G1``
"""
from __future__ import annotations

import argparse
import asyncio
import signal

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import build_plan
from src.tcanet.prototype.fabric import NetnsFabric, SimFabric
from src.tcanet.scenario_fig1 import load


def build_agent(role: str, agent_id: str, gateway: str, *, scenario: str, sim: bool, bus_path=None):
    from src.tcanet.prototype.agents.app import AppAgent
    from src.tcanet.prototype.agents.net import NetAgent
    from src.tcanet.prototype.agents.phy import PhyAgent
    from src.tcanet.prototype.agents.trans import TransAgent

    world, task = load(scenario)
    plan = build_plan(world)
    factory = SimFabric if sim else (lambda bus: NetnsFabric(plan))
    common = {"bus_path": bus_path or config.bus_path(), "fabric_factory": factory}
    if role == "application":
        endpoint = agent_id.removeprefix("app-")
        return AppAgent(agent_id, gateway, endpoint=endpoint, task=task, plan=plan, **common)
    if role == "network":
        return NetAgent(agent_id, gateway, world=world, plan=plan, **common)
    if role == "physical":
        return PhyAgent(agent_id, gateway, **common)
    if role == "transport":
        return TransAgent(agent_id, gateway, **common)
    raise ValueError(f"unknown role {role!r}")


async def _main(args: argparse.Namespace) -> None:
    agent = build_agent(args.role, args.agent_id, args.gateway, scenario=args.scenario, sim=args.sim)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await agent.run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet agent process")
    parser.add_argument("--role", required=True, choices=["application", "transport", "network", "physical"])
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--scenario", default="paper_fig1")
    parser.add_argument("--sim", action="store_true")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
