"""Privileged side of the prototype: data plane up/down and agent launch.

``sudo -E python -m src.tcanet.prototype.root up --mode netns`` builds the
namespaces and starts the broker, protected background traffic and all
agents inside their namespaces.  ``--mode sim`` (no root) starts the
simulated data plane instead.  ``down`` stops everything and removes the
namespaces.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import AddressPlan, build_plan, gateway_ns
from src.tcanet.prototype.background import BACKGROUND_PORT
from src.tcanet.prototype.runtime import ProcessTable, agent_argv, agent_specs
from src.tcanet.prototype.topology import build_commands, preflight, run_commands, teardown_commands
from src.tcanet.scenario_fig1 import ACCESS_CAPACITY_MBPS, load
from src.tcanet.spec import World

REPO_ROOT = Path(__file__).resolve().parents[3]


def process_plan(mode: str, world: World, plan: AddressPlan, scenario: str,
                 python: str = sys.executable, *, web: bool = False) -> list[tuple[str, list[str]]]:
    """``(name, argv)`` of every background process, in start order."""
    processes: list[tuple[str, list[str]]] = [("bus", [python, "-m", "src.tcanet.prototype.bus"])]
    if mode == "sim":
        processes.append(("simnet", [python, "-m", "src.tcanet.prototype.simnet", "--scenario", scenario]))
    else:
        for link_id in sorted(plan.links):
            protected = world.resources[f"link:{link_id}"].protected_load_mbps
            if protected <= 0:
                continue
            addr = plan.links[link_id]
            processes.append((f"bg-{link_id}-sink", [
                "ip", "netns", "exec", gateway_ns(addr.target_gw), python, "-m",
                "src.tcanet.prototype.background", "sink", "--port", str(BACKGROUND_PORT)]))
            processes.append((f"bg-{link_id}", [
                "ip", "netns", "exec", gateway_ns(addr.source_gw), python, "-m",
                "src.tcanet.prototype.background", "send", "--dst", addr.target_ip,
                "--port", str(BACKGROUND_PORT), "--mbps", f"{protected:g}"]))
    processes += [(spec.agent_id, agent_argv(spec, sim=mode == "sim", scenario=scenario, python=python))
                  for spec in agent_specs(world, plan)]
    if web:  # controller + web console run headless, driven from the browser
        processes += [("controller", [python, "-m", "src.tcanet.prototype.controller", "--scenario", scenario]),
                      ("web", [python, "-m", "src.tcanet.prototype.web"])]
    return processes


def hand_back(root: Path, env: dict[str, str] = os.environ, *, chown=os.chown) -> None:
    """Give the run tree back to the user who ran ``sudo`` (no-op otherwise).

    Without this, a later user-level ``up --sim`` cannot write the root-owned
    run dir, logs or event record left by a netns run.
    """
    if "SUDO_UID" not in env:
        return
    uid, gid = int(env["SUDO_UID"]), int(env.get("SUDO_GID", env["SUDO_UID"]))
    root = Path(root)
    for path in [root, *root.rglob("*")]:
        if path.is_socket():
            continue
        try:
            chown(path, uid, gid)
        except OSError:
            pass


def _shared_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o777)


def console_urls(port: int) -> list[str]:
    """Browser URLs of the web console (VM addresses first)."""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
    except FileNotFoundError:
        out = []
    addrs = [a for a in out if ":" not in a] or ["127.0.0.1"]
    return [f"http://{addr}:{port}" for addr in addrs]


def up(mode: str, scenario: str = "paper_fig1", *, web: bool = False) -> None:
    for path in (config.run_dir(), config.pids_dir(), config.logs_dir(), config.captures_dir()):
        _shared_dir(path)
    config.mode_path().write_text(mode, encoding="utf-8")
    world, _task = load(scenario)
    plan = build_plan(world)
    if mode == "netns":
        missing = preflight()
        if missing:
            raise SystemExit(f"missing tools: {', '.join(missing)} (apt install iproute2)")
        run_commands(teardown_commands(world, plan))
        run_commands(build_commands(world, plan, ACCESS_CAPACITY_MBPS))
    table = ProcessTable(config.pids_dir())
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}  # no root-owned __pycache__ in the repo
    for name, argv in process_plan(mode, world, plan, scenario, web=web):
        table.start(name, argv, log_path=config.logs_dir() / f"{name}.log", cwd=REPO_ROOT, env=env)
        if name == "bus":
            deadline = time.time() + 5.0
            while not config.bus_path().exists():
                if time.time() > deadline:
                    raise SystemExit("bus did not start; see logs/bus.log")
                time.sleep(0.05)
    time.sleep(0.5)  # let the bus create events.jsonl before handing the tree back
    hand_back(config.run_dir())
    print(f"TCANet prototype up ({mode}): {len(table.names())} processes, run dir {config.run_dir()}")
    if web:
        port = int(os.environ.get("TCANET_WEB_PORT", "8080"))
        print("Open the web console in a browser:  " + "   ".join(console_urls(port)))


def down() -> None:
    world, _task = load("paper_fig1")
    plan = build_plan(world)
    ProcessTable(config.pids_dir()).stop_all()
    if config.read_mode() == "netns" and os.geteuid() == 0:
        run_commands(teardown_commands(world, plan))
    config.bus_path().unlink(missing_ok=True)
    hand_back(config.run_dir())
    print("TCANet prototype down")


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet prototype data plane")
    sub = parser.add_subparsers(dest="op", required=True)
    p = sub.add_parser("up")
    p.add_argument("--mode", choices=["netns", "sim"], default="netns")
    p.add_argument("--scenario", default="paper_fig1")
    p.add_argument("--web", action="store_true", help="also run the controller and the web console")
    sub.add_parser("down")
    args = parser.parse_args()
    if args.op == "up":
        up(args.mode, args.scenario, web=args.web)
    else:
        down()


if __name__ == "__main__":
    main()
