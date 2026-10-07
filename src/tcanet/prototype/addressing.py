"""Address plan and forwarding-entry -> Linux policy-routing commands.

Each task-specific forwarding entry ``(e, next_g(pi_{m,e}))`` at gateway ``g``
(paper Eq. 3) becomes, inside ``g``'s network namespace::

    ip route replace <dst>/32 via <next-hop> dev <link> table <100+i>
    ip rule add from <src> to <dst> priority <1000+i> table <100+i>

so ``ip rule show`` on a gateway lists exactly its share of ``FT_m``.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.tcanet.prototype.config import FLOW_PORT_BASE
from src.tcanet.spec import TaskSpecification, World
from src.tcanet.subnet import ForwardingEntry


@dataclass(frozen=True)
class Cmd:
    """One shell command; ``check=False`` tolerates a non-zero exit."""

    argv: tuple[str, ...]
    check: bool = True

    def text(self) -> str:
        return " ".join(self.argv)


def in_ns(ns: str, cmd: Cmd) -> Cmd:
    return Cmd(("ip", "netns", "exec", ns, *cmd.argv), cmd.check)


def gateway_ns(gateway_id: str) -> str:
    return f"tc-{gateway_id.lower()}"


def endpoint_ns(agent_id: str) -> str:
    return f"tc-{agent_id.lower()}"


@dataclass(frozen=True)
class LinkAddr:
    link_id: str
    source_gw: str
    target_gw: str
    ifname: str
    source_ip: str
    target_ip: str


@dataclass(frozen=True)
class EndpointAddr:
    agent_id: str
    gateway_id: str
    ifname: str  # gateway-side access interface
    endpoint_ip: str
    gateway_ip: str


@dataclass(frozen=True)
class AddressPlan:
    links: dict[str, LinkAddr]
    endpoints: dict[str, EndpointAddr]

    def link_between(self, source_gw: str, target_gw: str) -> LinkAddr | None:
        for link_id in sorted(self.links):
            link = self.links[link_id]
            if link.source_gw == source_gw and link.target_gw == target_gw:
                return link
        return None

    def links_at(self, gateway_id: str) -> tuple[LinkAddr, ...]:
        return tuple(
            self.links[link_id]
            for link_id in sorted(self.links)
            if gateway_id in (self.links[link_id].source_gw, self.links[link_id].target_gw)
        )

    def endpoints_at(self, gateway_id: str) -> tuple[EndpointAddr, ...]:
        return tuple(
            self.endpoints[agent_id]
            for agent_id in sorted(self.endpoints)
            if self.endpoints[agent_id].gateway_id == gateway_id
        )


def build_plan(world: World) -> AddressPlan:
    """Deterministic addressing: link k -> 10.0.k.0/30, endpoint k -> 10.1.k.0/24."""
    links = {}
    for index, link in enumerate(
        sorted(world.graph.links, key=lambda item: item.link_id), start=1
    ):
        links[link.link_id] = LinkAddr(
            link_id=link.link_id,
            source_gw=link.source_gateway,
            target_gw=link.target_gateway,
            ifname=link.link_id.lower(),
            source_ip=f"10.0.{index}.1",
            target_ip=f"10.0.{index}.2",
        )
    endpoints = {}
    for index, agent_id in enumerate(sorted(world.endpoints), start=1):
        endpoint = world.endpoints[agent_id]
        endpoints[agent_id] = EndpointAddr(
            agent_id=agent_id,
            gateway_id=endpoint.gateway_id,
            ifname=f"acc-{agent_id.lower()}",
            endpoint_ip=f"10.1.{index}.2",
            gateway_ip=f"10.1.{index}.1",
        )
    return AddressPlan(links=links, endpoints=endpoints)


def dep_index(task: TaskSpecification, dep_id: str) -> int:
    return sorted(dep.dep_id for dep in task.dag.dependencies).index(dep_id)


def dep_table(task: TaskSpecification, dep_id: str) -> int:
    return 100 + dep_index(task, dep_id)


def dep_priority(task: TaskSpecification, dep_id: str) -> int:
    return 1000 + dep_index(task, dep_id)


def flow_port(task: TaskSpecification, dep_id: str) -> int:
    return FLOW_PORT_BASE + dep_index(task, dep_id)


def rule_params(
    entry: ForwardingEntry, task: TaskSpecification, plan: AddressPlan
) -> dict:
    """Concrete, JSON-serializable parameters of one ``FT^g_m`` entry."""
    src = plan.endpoints[entry.src_agent]
    dst = plan.endpoints[entry.dst_agent]
    params = {
        "gateway": entry.gateway_id,
        "dep_id": entry.dep_id,
        "rule_id": entry.rule_id,
        "src_ip": src.endpoint_ip,
        "dst_ip": dst.endpoint_ip,
        "table": dep_table(task, entry.dep_id),
        "priority": dep_priority(task, entry.dep_id),
        "mode": entry.action_mode,
        "next_hop_gateway": entry.next_hop_gateway,
        "via": None,
        "link_id": None,
    }
    if entry.action_mode == "local_delivery":
        params["dev"] = dst.ifname
        return params
    link = plan.link_between(entry.gateway_id, entry.next_hop_gateway or "")
    if link is None:
        raise ValueError(
            f"no link {entry.gateway_id}->{entry.next_hop_gateway} for {entry.rule_id}"
        )
    params.update(dev=link.ifname, via=link.target_ip, link_id=link.link_id)
    return params


def rule_commands(params: dict, op: str) -> list[Cmd]:
    """``op="add"`` installs (idempotently), ``op="del"`` withdraws."""
    table = str(params["table"])
    priority = str(params["priority"])
    dst = params["dst_ip"]
    selector = ("from", params["src_ip"], "to", dst, "priority", priority, "table", table)
    if op == "add":
        route = ("ip", "route", "replace", f"{dst}/32")
        if params.get("via"):
            route += ("via", params["via"])
        route += ("dev", params["dev"], "table", table)
        return [
            Cmd(route),
            Cmd(("ip", "rule", "del", *selector), check=False),
            Cmd(("ip", "rule", "add", *selector)),
        ]
    if op == "del":
        return [
            Cmd(("ip", "rule", "del", *selector), check=False),
            Cmd(("ip", "route", "del", f"{dst}/32", "table", table), check=False),
        ]
    raise ValueError(f"unknown op {op!r}")
