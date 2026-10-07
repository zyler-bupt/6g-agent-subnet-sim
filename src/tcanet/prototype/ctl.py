"""``tcanetctl`` — orchestrator input and operator fault injection.

Faults act on the kernel (netns mode) or the simulator (sim mode) and on
agent processes; they are never announced to the controller, which learns
of them only from agent reports.  Each operator action is also published
on ``ops.fault`` for the event record (the controller does not subscribe).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import AddressPlan, Cmd, build_plan
from src.tcanet.prototype.bus import BusClient
from src.tcanet.prototype.runtime import SUPPORT_ROLES, ProcessTable, agent_argv, agent_specs
from src.tcanet.prototype.topology import (
    gateway_state_commands,
    link_qdisc,
    link_state_command,
    run_commands,
)
from src.tcanet.scenario_fig1 import load
from src.tcanet.spec import World

REPO_ROOT = Path(__file__).resolve().parents[3]


class Runtime(Protocol):
    mode: str

    async def kernel(self, cmds: list[Cmd]) -> None: ...

    def kill(self, agent_id: str) -> bool: ...

    def revive(self, agent_id: str) -> bool: ...


class ProcessRuntime:
    def __init__(self, mode: str, world: World, plan: AddressPlan, scenario: str) -> None:
        self.mode = mode
        self.table = ProcessTable(config.pids_dir())
        self.specs = {spec.agent_id: spec for spec in agent_specs(world, plan)}
        self.scenario = scenario

    async def kernel(self, cmds: list[Cmd]) -> None:
        await asyncio.to_thread(run_commands, cmds)

    def kill(self, agent_id: str) -> bool:
        return self.table.kill(agent_id)

    def revive(self, agent_id: str) -> bool:
        if self.table.alive(agent_id) or agent_id not in self.specs:
            return False
        argv = agent_argv(self.specs[agent_id], sim=self.mode == "sim", scenario=self.scenario)
        self.table.start(agent_id, argv, log_path=config.logs_dir() / f"{agent_id}.log",
                         cwd=REPO_ROOT, env=dict(os.environ))
        return True


async def _record(bus: BusClient, op: str, **fields) -> None:
    await bus.publish("ops.fault", {"op": op, **fields})


async def submit(bus: BusClient, scenario: str) -> None:
    await bus.publish("task.submit", {"scenario": scenario})
    await _record(bus, "submit", scenario=scenario)


async def withdraw(bus: BusClient) -> None:
    await bus.publish("task.withdraw", {})
    await _record(bus, "withdraw")


async def demand(bus: BusClient, dep_id: str, mbps: float) -> None:
    await bus.publish("ctl.demand", {"dep_id": dep_id, "mbps": mbps})
    await _record(bus, "demand", dep_id=dep_id, mbps=mbps)


async def set_gateway(bus: BusClient, rt: Runtime, plan: AddressPlan, gateway: str, up: bool) -> None:
    if rt.mode == "sim":
        await bus.publish("sim.fault", {"kind": "gateway", "gateway": gateway, "up": up})
    else:
        await rt.kernel(gateway_state_commands(plan, gateway, up))
    for role in SUPPORT_ROLES:
        (rt.revive if up else rt.kill)(f"{role}-{gateway}")
    await _record(bus, "gateway", target=gateway, up=up)


async def set_link(bus: BusClient, rt: Runtime, plan: AddressPlan, link_id: str, up: bool) -> None:
    if rt.mode == "sim":
        await bus.publish("sim.fault", {"kind": "link", "link_id": link_id, "up": up})
    else:
        await rt.kernel([link_state_command(plan, link_id, up)])
    await _record(bus, "link", target=link_id, up=up)


async def degrade(bus: BusClient, rt: Runtime, world: World, plan: AddressPlan, link_id: str, loss_pct: float) -> None:
    if rt.mode == "sim":
        await bus.publish("sim.fault", {"kind": "degrade", "link_id": link_id, "loss": loss_pct / 100.0})
    else:
        await rt.kernel([link_qdisc(world, plan, link_id, loss_pct=loss_pct)])
    await _record(bus, "degrade", target=link_id, loss_pct=loss_pct)


async def kill(bus: BusClient, rt: Runtime, agent_id: str) -> bool:
    killed = rt.kill(agent_id)
    await _record(bus, "kill", target=agent_id)
    return killed


async def revive(bus: BusClient, rt: Runtime, agent_id: str) -> bool:
    revived = rt.revive(agent_id)
    await _record(bus, "revive", target=agent_id)
    return revived


async def reset(bus: BusClient, rt: Runtime, world: World, plan: AddressPlan) -> None:
    """Heal every injected fault, revive dead agents, withdraw the task."""
    if rt.mode == "sim":
        await bus.publish("sim.fault", {"kind": "reset"})
    else:
        cmds = [cmd for gateway in world.graph.gateways for cmd in gateway_state_commands(plan, gateway, True)]
        cmds += [link_state_command(plan, link_id, True) for link_id in sorted(plan.links)]
        cmds += [link_qdisc(world, plan, link_id) for link_id in sorted(plan.links)]
        await rt.kernel(cmds)
    for agent_id in sorted(getattr(rt, "specs", {})):
        rt.revive(agent_id)
    await asyncio.sleep(3 * config.sample_period_s())  # let stale link reports drain
    await withdraw(bus)
    await _record(bus, "reset")


def status_lines(world: World, plan: AddressPlan) -> list[str]:
    table = ProcessTable(config.pids_dir())
    lines = [f"mode: {config.read_mode()}   run dir: {config.run_dir()}"]
    for spec in agent_specs(world, plan):
        lines.append(f"  {spec.agent_id:<13} {'alive' if table.alive(spec.agent_id) else 'DEAD'}")
    if config.read_mode() == "netns":
        for gateway in world.graph.gateways:
            out = subprocess.run(["ip", "-n", f"tc-{gateway.lower()}", "rule", "show"],
                                 capture_output=True, text=True).stdout
            rules = [line for line in out.splitlines() if "lookup 1" in line]
            lines.append(f"  {gateway}: {len(rules)} task FT rule(s)")
    return lines


_ROOT_OPS = {"fail", "restore", "degrade", "clear", "kill", "revive", "reset"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tcanetctl", description="TCANet prototype control")
    sub = parser.add_subparsers(dest="op", required=True)
    sub.add_parser("submit").add_argument("scenario", nargs="?", default="paper_fig1")
    sub.add_parser("withdraw")
    p = sub.add_parser("demand")
    p.add_argument("dep_id")
    p.add_argument("mbps", type=float)
    for name in ("fail", "restore"):
        p = sub.add_parser(name)
        p.add_argument("kind", choices=["gateway", "link"])
        p.add_argument("target")
    p = sub.add_parser("degrade")
    p.add_argument("link_id")
    p.add_argument("loss_pct", type=float)
    sub.add_parser("clear").add_argument("link_id")
    sub.add_parser("kill").add_argument("agent_id")
    sub.add_parser("revive").add_argument("agent_id")
    sub.add_parser("reset")
    sub.add_parser("status")
    return parser


async def run(args: argparse.Namespace, bus: BusClient, rt: Runtime, world: World, plan: AddressPlan) -> None:
    op = args.op
    if op == "submit":
        await submit(bus, args.scenario)
    elif op == "withdraw":
        await withdraw(bus)
    elif op == "demand":
        await demand(bus, args.dep_id, args.mbps)
    elif op in ("fail", "restore"):
        up = op == "restore"
        if args.kind == "gateway":
            await set_gateway(bus, rt, plan, args.target, up)
        else:
            await set_link(bus, rt, plan, args.target, up)
    elif op == "degrade":
        await degrade(bus, rt, world, plan, args.link_id, args.loss_pct)
    elif op == "clear":
        await degrade(bus, rt, world, plan, args.link_id, 0.0)
    elif op == "kill":
        await kill(bus, rt, args.agent_id)
    elif op == "revive":
        await revive(bus, rt, args.agent_id)
    elif op == "reset":
        await reset(bus, rt, world, plan)


async def _main(args: argparse.Namespace) -> None:
    world, _task = load("paper_fig1")
    plan = build_plan(world)
    if args.op == "status":
        print("\n".join(status_lines(world, plan)))
        return
    bus = await BusClient.connect(config.bus_path(), "tcanetctl")
    rt = ProcessRuntime(config.read_mode(), world, plan, "paper_fig1")
    await run(args, bus, rt, world, plan)
    await asyncio.sleep(0.05)  # let the last frame flush
    await bus.close()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if args.op in _ROOT_OPS and config.read_mode() == "netns" and os.geteuid() != 0:
        os.execvp("sudo", ["sudo", "-E", sys.executable, "-m", "src.tcanet.prototype.ctl", *argv])
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
