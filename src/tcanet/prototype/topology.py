"""Network-namespace topology for the prototype data plane.

Builds one namespace per gateway (``tc-g1``...) and per task endpoint
(``tc-a1``...).  Every gateway link becomes a veth pair shaped with
``tc netem`` (delay + rate) on its source side; every endpoint attaches to
its gateway through an access veth shaped in both directions.  No
endpoint routes are installed in the gateways' main tables, so a task flow
is forwarded only by the per-dependency FT rules the controller installs.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Callable

from src.tcanet.prototype.addressing import (
    AddressPlan,
    Cmd,
    endpoint_ns,
    gateway_ns,
    in_ns,
)
from src.tcanet.spec import World

LINK_QUEUE_LIMIT = 10000
ACCESS_DELAY_MS = 1.0
REQUIRED_TOOLS = ("ip", "tc", "sysctl")


def netem_args(delay_ms: float, rate_mbps: float, loss_pct: float = 0.0) -> tuple[str, ...]:
    args: tuple[str, ...] = (
        "netem", "delay", f"{delay_ms:g}ms", "rate", f"{rate_mbps:g}mbit",
    )
    if loss_pct > 0.0:
        args += ("loss", f"{loss_pct:g}%")
    return args + ("limit", str(LINK_QUEUE_LIMIT))


def link_qdisc(
    world: World, plan: AddressPlan, link_id: str, *, loss_pct: float = 0.0, op: str = "replace"
) -> Cmd:
    """Shaping of a link's forward direction (source-side egress)."""
    addr = plan.links[link_id]
    link = world.graph.link(link_id)
    return in_ns(
        gateway_ns(addr.source_gw),
        Cmd(("tc", "qdisc", op, "dev", addr.ifname, "root",
             *netem_args(link.delay_ms, link.capacity_mbps, loss_pct))),
    )


def access_qdiscs(plan: AddressPlan, agent_id: str, rate_mbps: float, *, op: str = "replace") -> list[Cmd]:
    """Downlink (gateway egress) and uplink (endpoint egress) access shaping."""
    endpoint = plan.endpoints[agent_id]
    args = netem_args(ACCESS_DELAY_MS, rate_mbps)
    return [
        in_ns(gateway_ns(endpoint.gateway_id),
              Cmd(("tc", "qdisc", op, "dev", endpoint.ifname, "root", *args))),
        in_ns(endpoint_ns(agent_id),
              Cmd(("tc", "qdisc", op, "dev", "eth0", "root", *args))),
    ]


def namespaces(world: World, plan: AddressPlan) -> list[str]:
    return [gateway_ns(g) for g in world.graph.gateways] + [
        endpoint_ns(agent_id) for agent_id in sorted(plan.endpoints)
    ]


def build_commands(world: World, plan: AddressPlan, access_mbps: float) -> list[Cmd]:
    cmds: list[Cmd] = []
    for ns in namespaces(world, plan):
        cmds.append(Cmd(("ip", "netns", "add", ns)))
        cmds.append(in_ns(ns, Cmd(("ip", "link", "set", "lo", "up"))))
    for gateway in world.graph.gateways:
        ns = gateway_ns(gateway)
        for key in ("net.ipv4.ip_forward=1",
                    "net.ipv4.conf.all.rp_filter=0",
                    "net.ipv4.conf.default.rp_filter=0"):
            cmds.append(in_ns(ns, Cmd(("sysctl", "-qw", key))))
    for link_id in sorted(plan.links):
        addr = plan.links[link_id]
        src_ns, dst_ns = gateway_ns(addr.source_gw), gateway_ns(addr.target_gw)
        tmp_a, tmp_b = f"t{addr.ifname}a", f"t{addr.ifname}b"
        cmds += [
            Cmd(("ip", "link", "add", tmp_a, "type", "veth", "peer", "name", tmp_b)),
            Cmd(("ip", "link", "set", tmp_a, "netns", src_ns)),
            Cmd(("ip", "link", "set", tmp_b, "netns", dst_ns)),
            in_ns(src_ns, Cmd(("ip", "link", "set", tmp_a, "name", addr.ifname))),
            in_ns(dst_ns, Cmd(("ip", "link", "set", tmp_b, "name", addr.ifname))),
            in_ns(src_ns, Cmd(("ip", "addr", "add", f"{addr.source_ip}/30", "dev", addr.ifname))),
            in_ns(dst_ns, Cmd(("ip", "addr", "add", f"{addr.target_ip}/30", "dev", addr.ifname))),
            in_ns(src_ns, Cmd(("ip", "link", "set", addr.ifname, "up"))),
            in_ns(dst_ns, Cmd(("ip", "link", "set", addr.ifname, "up"))),
            link_qdisc(world, plan, link_id, op="add"),
        ]
    for agent_id in sorted(plan.endpoints):
        endpoint = plan.endpoints[agent_id]
        gw_ns, ep_ns = gateway_ns(endpoint.gateway_id), endpoint_ns(agent_id)
        tmp_g, tmp_e = f"t{endpoint.ifname}g", f"t{endpoint.ifname}e"
        cmds += [
            Cmd(("ip", "link", "add", tmp_g, "type", "veth", "peer", "name", tmp_e)),
            Cmd(("ip", "link", "set", tmp_g, "netns", gw_ns)),
            Cmd(("ip", "link", "set", tmp_e, "netns", ep_ns)),
            in_ns(gw_ns, Cmd(("ip", "link", "set", tmp_g, "name", endpoint.ifname))),
            in_ns(ep_ns, Cmd(("ip", "link", "set", tmp_e, "name", "eth0"))),
            in_ns(gw_ns, Cmd(("ip", "addr", "add", f"{endpoint.gateway_ip}/24", "dev", endpoint.ifname))),
            in_ns(ep_ns, Cmd(("ip", "addr", "add", f"{endpoint.endpoint_ip}/24", "dev", "eth0"))),
            in_ns(gw_ns, Cmd(("ip", "link", "set", endpoint.ifname, "up"))),
            in_ns(ep_ns, Cmd(("ip", "link", "set", "eth0", "up"))),
            in_ns(ep_ns, Cmd(("ip", "route", "add", "default", "via", endpoint.gateway_ip))),
            *access_qdiscs(plan, agent_id, access_mbps, op="add"),
        ]
    return cmds


def teardown_commands(world: World, plan: AddressPlan) -> list[Cmd]:
    return [Cmd(("ip", "netns", "del", ns), check=False) for ns in namespaces(world, plan)]


def gateway_state_commands(plan: AddressPlan, gateway_id: str, up: bool) -> list[Cmd]:
    """Take every interface of a gateway down (gateway failure) or up."""
    state = "up" if up else "down"
    ifnames = [link.ifname for link in plan.links_at(gateway_id)] + [
        endpoint.ifname for endpoint in plan.endpoints_at(gateway_id)
    ]
    return [
        in_ns(gateway_ns(gateway_id), Cmd(("ip", "link", "set", ifname, state)))
        for ifname in ifnames
    ]


def link_state_command(plan: AddressPlan, link_id: str, up: bool) -> Cmd:
    addr = plan.links[link_id]
    return in_ns(
        gateway_ns(addr.source_gw),
        Cmd(("ip", "link", "set", addr.ifname, "up" if up else "down")),
    )


Runner = Callable[..., subprocess.CompletedProcess]


def run_commands(cmds: list[Cmd], *, dry_run: bool = False, runner: Runner = subprocess.run) -> list[str]:
    """Execute ``cmds`` in order; raise on the first failing checked command."""
    lines: list[str] = []
    for cmd in cmds:
        lines.append(cmd.text())
        if dry_run:
            continue
        proc = runner(list(cmd.argv), capture_output=True, text=True)
        if cmd.check and proc.returncode != 0:
            raise RuntimeError(f"{cmd.text()} failed: {proc.stderr.strip()}")
    return lines


def preflight(which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """Names of required tools missing from PATH."""
    return [tool for tool in REQUIRED_TOOLS if which(tool) is None]
