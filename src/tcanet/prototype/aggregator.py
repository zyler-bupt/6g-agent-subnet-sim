"""Controller-side state from agent reports (``s_m``) and event detection.

``StateAggregator`` is the controller's only view of the network: link
liveness, access capacity, agent heartbeats, flow samples and application
demand all come from agent reports.  ``EventDetector`` turns the evidence
into the runtime events of paper Sec. IV-B, debouncing so that the two
neighbours of a failed gateway (which report one sampling period apart)
yield a single ``gateway_failure`` instead of two ``link_failure`` events.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field, replace

from src.tcanet.closure import (
    RuntimeEvent,
    demand_change,
    gateway_failure,
    link_failure,
    support_failure,
)
from src.tcanet.scenario import fail_gateway, fail_support_agent
from src.tcanet.spec import SharedResource, World


@dataclass(frozen=True)
class Evidence:
    kind: str  # link_down | link_up | agent_silent | demand
    subject: str
    ts: float
    detail: dict = field(default_factory=dict)


class FlowBook:
    """Recent per-dependency flow samples keyed by report timestamp."""

    def __init__(self, horizon_s: float = 120.0) -> None:
        self.horizon_s = horizon_s
        self._samples: dict[str, deque[tuple[float, dict]]] = defaultdict(deque)

    def add(self, dep_id: str, ts: float, sample: dict) -> None:
        samples = self._samples[dep_id]
        samples.append((ts, sample))
        while samples and samples[0][0] < ts - self.horizon_s:
            samples.popleft()

    def since(self, dep_id: str, t0: float) -> list[tuple[float, dict]]:
        return [(ts, sample) for ts, sample in self._samples.get(dep_id, ()) if ts >= t0]

    def latest(self, dep_id: str) -> dict | None:
        samples = self._samples.get(dep_id)
        return samples[-1][1] if samples else None


def _set_link_up(world: World, link_id: str, up: bool) -> None:
    world.graph = replace(
        world.graph,
        links=tuple(
            replace(link, up=up) if link.link_id == link_id else link
            for link in world.graph.links
        ),
    )


class StateAggregator:
    def __init__(self, world: World) -> None:
        self.world = world
        self.flowbook = FlowBook()
        self.last_seen: dict[str, float] = {}
        self.silent: set[str] = set()
        self.link_reports: dict[str, dict[str, bool]] = defaultdict(dict)
        self.demands: dict[str, float] = {}
        self.access: dict[str, dict] = {}

    def apply(self, msg: dict) -> list[Evidence]:
        topic, payload, ts = msg["topic"], msg["payload"], float(msg["ts"])
        if topic in ("hello", "heartbeat"):
            agent_id = payload.get("agent_id", msg["src"])
            self.last_seen[agent_id] = ts
            self.silent.discard(agent_id)
            agent = self.world.support_agents.get(agent_id)
            if agent is not None and not agent.online and agent.gateway_id not in self.world.failed_gateways:
                self.world.support_agents[agent_id] = replace(agent, online=True)
            return []
        if topic == "report.link":
            return self._link(payload, ts)
        if topic == "report.access":
            self._access(payload)
            return []
        if topic == "report.flow":
            self.flowbook.add(payload["dep_id"], ts, payload)
            return []
        if topic == "report.app":
            dep_id, new = payload["dep_id"], float(payload["demand_mbps"])
            old = self.demands.get(dep_id)
            self.demands[dep_id] = new
            if old is not None and abs(new - old) > 1e-6:
                return [Evidence("demand", dep_id, ts, {"old": old, "new": new})]
        return []

    def link_up(self, link_id: str) -> bool:
        reports = self.link_reports.get(link_id)
        return all(reports.values()) if reports else True

    def _link(self, payload: dict, ts: float) -> list[Evidence]:
        link_id = payload["link_id"]
        before = self.link_up(link_id)
        self.link_reports[link_id][payload["reporter"]] = bool(payload["up"])
        after = self.link_up(link_id)
        if before and not after:
            _set_link_up(self.world, link_id, False)
            return [Evidence("link_down", link_id, ts, {"reporter": payload["reporter"]})]
        if not before and after:
            link = self.world.graph.link(link_id)
            for gateway in (link.source_gateway, link.target_gateway):
                if gateway in self.world.failed_gateways and self._gateway_links_up(gateway):
                    self._restore_gateway(gateway)
            if not ({link.source_gateway, link.target_gateway} & self.world.failed_gateways):
                _set_link_up(self.world, link_id, True)
            return [Evidence("link_up", link_id, ts)]
        return []

    def _gateway_links_up(self, gateway: str) -> bool:
        return all(
            self.link_up(link.link_id)
            for link in self.world.graph.links
            if gateway in (link.source_gateway, link.target_gateway)
        )

    def _restore_gateway(self, gateway: str) -> None:
        """A failed gateway whose links all report up again is live again."""
        self.world.failed_gateways.discard(gateway)
        for link in self.world.graph.links:
            if gateway in (link.source_gateway, link.target_gateway):
                _set_link_up(self.world, link.link_id, True)
        for agent_id, agent in self.world.support_agents.items():
            if agent.gateway_id == gateway and agent_id not in self.silent:
                self.world.support_agents[agent_id] = replace(agent, online=True)

    def _access(self, payload: dict) -> None:
        gateway, capacity = payload["gateway"], float(payload["capacity_mbps"])
        self.access[gateway] = payload
        for resource_id in (f"access-ul:{gateway}", f"access-dl:{gateway}", f"access:{gateway}"):
            if resource_id in self.world.resources:
                self.world.resources[resource_id] = SharedResource(resource_id, capacity)

    def silent_agents(self, now: float, timeout_s: float) -> list[Evidence]:
        found = []
        for agent_id, seen in sorted(self.last_seen.items()):
            if agent_id not in self.silent and now - seen > timeout_s:
                self.silent.add(agent_id)
                found.append(Evidence("agent_silent", agent_id, seen + timeout_s))
        return found


class EventDetector:
    def __init__(self, world: World, *, debounce_s: float) -> None:
        self.world = world
        self.debounce_s = debounce_s
        self._pending: list[Evidence] = []
        self._ready: list[tuple[RuntimeEvent, float]] = []

    def feed(self, evidences: list[Evidence]) -> None:
        for evidence in evidences:
            if evidence.kind == "demand":
                factor = evidence.detail["new"] / max(evidence.detail["old"], 1e-9)
                self._ready.append((demand_change(evidence.subject, factor), evidence.ts))
            elif evidence.kind in ("link_down", "agent_silent"):
                self._pending.append(evidence)

    def poll(self, now: float) -> list[tuple[RuntimeEvent, float]]:
        ready, self._ready = self._ready, []
        if self._pending and now - min(e.ts for e in self._pending) >= self.debounce_s:
            ready += self._classify()
            self._pending = []
        return ready

    def _adjacent(self, gateway: str) -> set[str]:
        return {
            link.link_id for link in self.world.graph.links
            if gateway in (link.source_gateway, link.target_gateway)
        }

    def _classify(self) -> list[tuple[RuntimeEvent, float]]:
        events: list[tuple[RuntimeEvent, float]] = []
        down_now = {link.link_id for link in self.world.graph.links if not link.up}
        link_evidence = {e.subject: e for e in self._pending if e.kind == "link_down"}
        first_ts = min(e.ts for e in self._pending)
        covered: set[str] = set()
        for gateway in self.world.graph.gateways:
            adjacent = self._adjacent(gateway)
            if (
                gateway not in self.world.failed_gateways
                and adjacent
                and adjacent <= down_now
                and adjacent & set(link_evidence)
            ):
                fail_gateway(self.world, gateway)
                events.append((gateway_failure(gateway), first_ts))
                covered |= adjacent
        for link_id in sorted(set(link_evidence) - covered):
            events.append((link_failure(link_id), link_evidence[link_id].ts))
        for evidence in self._pending:
            if evidence.kind != "agent_silent":
                continue
            agent = self.world.support_agents.get(evidence.subject)
            if agent is None or agent.gateway_id in self.world.failed_gateways or not agent.online:
                continue
            fail_support_agent(self.world, evidence.subject)
            events.append((support_failure(evidence.subject), evidence.ts))
        return events
