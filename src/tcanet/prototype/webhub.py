"""State hub behind the web console (no I/O, so it is testable).

Consumes bus messages and keeps what the browser needs to draw the live
system: the active subnet, the Algorithm 1 log, flow / access series,
link and agent liveness, per-layer agent logs, fault markers and KPIs.
It also condenses the controller's log into one plain-English caption per
episode ("Gateway G2 failed → scope {e1, e2}, e3 untouched → …").
"""
from __future__ import annotations

import re
from collections import defaultdict, deque

from src.tcanet.scenario_fig1 import load
from src.tcanet.spec import TaskSpecification

FORWARD_TOPICS = ("report.flow", "report.access", "ctrl.log", "ctrl.subnet", "ctrl.metrics",
                  "agent.log", "ops.fault")
SUBSCRIBE_TOPICS = FORWARD_TOPICS + ("report.link", "report.app", "hello", "heartbeat")
LAYERS = ("application", "transport", "network", "physical")

_SERIES_MAX = 400
_LOG_MAX = 200
_AGENT_LOG_MAX = 80


def plain_action(action_id: str) -> str:
    """``network:REROUTE:e1:G1|G3|G4`` -> ``reroute e1 via G1→G3→G4``."""
    parts = action_id.split(":")
    if len(parts) < 4:
        return action_id
    _layer, verb, target, arg = parts[0], parts[1], parts[2], ":".join(parts[3:])
    if verb == "REROUTE":
        return f"reroute {target} via {arg.replace('|', '→')}"
    if verb == "ADJUST_RATE":
        return f"set {target} source rate to {arg} Mbps"
    if verb == "SWITCH_MODE":
        return f"switch {target} transport to {arg}"
    if verb == "REBIND":
        return f"rebind {target} to {arg}"
    if verb == "BOOST_ACCESS":
        return f"boost access at {target} by {arg} Mbps"
    return action_id


def _selected_label(lines: list[str]) -> str | None:
    for line in lines:
        if line.startswith("c* = "):
            label = line[len("c* = "):].split("  (")[0]
            return " + ".join(plain_action(part.strip()) for part in label.split(" + "))
    return None


class WebHub:
    def __init__(self, scenario: str = "paper_fig1", *, mode: str = "netns",
                 heartbeat_timeout_s: float = 1.5) -> None:
        self.world, self.task = load(scenario)
        self.scenario = scenario
        self.mode = mode
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.agent_ids = (
            [f"app-{agent_id}" for agent_id in sorted(self.world.endpoints)]
            + sorted(self.world.support_agents)
        )
        self.subnet: dict = {"version": 0, "paths": {}, "bindings": {}, "forwarding": []}
        self.logs: deque[dict] = deque(maxlen=_LOG_MAX)
        self.flows: dict[str, deque] = defaultdict(lambda: deque(maxlen=_SERIES_MAX))
        self.access: dict[str, deque] = defaultdict(lambda: deque(maxlen=_SERIES_MAX))
        self.agent_logs: dict[str, deque] = {layer: deque(maxlen=_AGENT_LOG_MAX) for layer in LAYERS}
        self.faults: deque[dict] = deque(maxlen=40)
        self.link_reports: dict[str, dict[str, bool]] = defaultdict(dict)
        self.link_util: dict[str, float] = {link.link_id: 0.0 for link in self.world.graph.links}
        self.last_seen: dict[str, float] = {}
        self.demand: dict[str, dict] = {}
        self.metrics: dict[str, dict] = {}
        self.affected: list[str] = []
        self.reconfiguring = False
        self.caption_parts: list[str] = ["Ready. Press ① to form the task subnet."]

    # ------------------------------------------------------------------
    def apply(self, msg: dict) -> dict | None:
        """Ingest one bus message; return the event to forward (or None)."""
        topic, p, ts = msg["topic"], msg["payload"], float(msg["ts"])
        if topic in ("hello", "heartbeat"):
            self.last_seen[p.get("agent_id", msg.get("src", ""))] = ts
            return None
        if topic == "report.link":
            self.link_reports[p["link_id"]][p["reporter"]] = bool(p["up"])
            if p.get("utilization") is not None:
                self.link_util[p["link_id"]] = float(p["utilization"])
            return None
        if topic == "report.app":
            self.demand[p["dep_id"]] = {"demand": p["demand_mbps"], "rate": p["rate_mbps"],
                                        "mode": p.get("mode", "standard")}
            return None
        if topic == "report.flow":
            self.flows[p["dep_id"]].append((ts, p["rx_mbps"], p.get("owd_ms"), p.get("loss")))
        elif topic == "report.access":
            self.access[p["gateway"]].append((ts, p["capacity_mbps"], p.get("snr_db")))
        elif topic == "ctrl.subnet":
            self.subnet = {key: p.get(key) for key in ("version", "paths", "bindings", "forwarding")}
        elif topic == "ctrl.metrics":
            self.metrics[p["kind"]] = p
        elif topic == "agent.log":
            layer = p.get("role")
            if layer in self.agent_logs:
                self.agent_logs[layer].append({"ts": ts, **p})
        elif topic == "ops.fault":
            self.faults.append({"ts": ts, **p})
        elif topic == "ctrl.log":
            self.logs.append({"ts": ts, **p})
            self._caption(p)
        else:
            return None
        return {"type": "msg", "topic": topic, "ts": ts, "payload": p, "caption": self.caption}

    # ------------------------------------------------------------------
    @property
    def caption(self) -> str:
        return " → ".join(self.caption_parts)

    def _caption(self, entry: dict) -> None:
        step, title, data = entry["step"], entry["title"], entry.get("data") or {}
        lines = entry.get("lines") or []
        if step == "formation" and title.startswith("T_m received"):
            deps = ", ".join(dep.dep_id for dep in self.task.dag.dependencies)
            self.caption_parts = [f"Task submitted: building a subnet for {deps}"]
            self.reconfiguring = True
        elif step == "event" and title.startswith("Event: "):
            self.caption_parts = [self._event_headline(title[len("Event: "):], data.get("kind", ""))]
            self.reconfiguring = True
        elif step == "scope":
            self.affected = list(data.get("affected", []))
            kept = sorted({d.dep_id for d in self.task.dag.dependencies} - set(self.affected))
            text = "scope {" + ", ".join(self.affected) + "}"
            if kept:
                text += ", " + ", ".join(kept) + " untouched"
            self.caption_parts.append(text)
        elif step == "select":
            label = _selected_label(lines)
            self.caption_parts.append(label or "no feasible configuration")
        elif step == "assess":
            if "PASS" in title:
                self.caption_parts.append("measured QoS passed")
            else:
                found = re.findall(r"(throughput|delay|loss)_hard:(e\d+)", title)
                why = ", ".join(f"{kind} on {dep}" for kind, dep in found) or "QoS violated"
                self.caption_parts.append(f"measured QoS failed ({why})")
        elif step == "rollback" and title.startswith("Rollback"):
            self.caption_parts.append("rolled back, trying next candidate")
        elif step == "commit":
            if entry.get("level") == "ok":
                latency = data.get("latency_ms")
                tail = f" in {latency / 1000:.1f} s" if latency is not None else ""
                self.caption_parts.append(f"committed v{data.get('version', '?')}{tail}")
            else:
                self.caption_parts.append("reconfiguration failed, previous version kept")
            self.affected = []
            self.reconfiguring = False
        elif step == "withdraw":
            self.caption_parts = ["Task withdrawn; network reset. Press ① to form the subnet again."]
            self.affected = []
            self.reconfiguring = False
        if len(self.caption_parts) > 10:
            self.caption_parts = self.caption_parts[:1] + ["…"] + self.caption_parts[-8:]

    def _event_headline(self, what: str, kind: str) -> str:
        """Plain-language headline for a runtime event (paper Sec. IV-B)."""
        target = what.rsplit(" ", 1)[-1]
        if kind == "gateway_failure":
            return f"Gateway {target} failed, detected by its neighbours' NetAgents"
        if kind == "link_failure":
            return f"Link {target} failed, detected by a NetAgent"
        if kind == "support_failure":
            return f"Supporting agent {target} stopped answering heartbeats"
        if kind == "demand_change":
            dep = what.split(":")[0].replace("dependency", "").strip()
            mbps = self.demand.get(dep, {}).get("demand")
            new = f"{mbps:g} Mbps" if isinstance(mbps, (int, float)) else "a new rate"
            return f"Task update: {dep} now needs {new}"
        return what[:1].upper() + what[1:]

    # ------------------------------------------------------------------
    def status(self, now: float) -> dict:
        agents = {
            agent_id: now - self.last_seen.get(agent_id, -1e9) <= self.heartbeat_timeout_s
            for agent_id in self.agent_ids
        }
        links = {
            link_id: {
                "up": all(self.link_reports[link_id].values()) if self.link_reports.get(link_id) else True,
                "util": round(self.link_util.get(link_id, 0.0), 3),
            }
            for link_id in sorted(self.link_util)
        }
        # A gateway is failed when every one of its links is reported down
        # (the same evidence the controller's event detector uses).
        failed = sorted(
            gw for gw in self.world.graph.gateways
            if all(not links[link.link_id]["up"] for link in self.world.graph.links
                   if gw in (link.source_gateway, link.target_gateway))
        )
        latest = {
            dep: {"rx": series[-1][1], "owd": series[-1][2], "loss": series[-1][3]}
            for dep, series in self.flows.items() if series
        }
        return {
            "type": "status", "now": now, "mode": self.mode, "caption": self.caption,
            "agents": agents, "online": sum(agents.values()), "total": len(agents),
            "links": links, "version": self.subnet.get("version", 0),
            "affected": self.affected, "reconfiguring": self.reconfiguring,
            "failed_gateways": failed,
            "qos": {dep.dep_id: self.qos_status(dep.dep_id) for dep in self.task.dag.dependencies},
            "latest": latest, "demand": self.demand, "metrics": self.metrics,
        }

    def qos_status(self, dep_id: str) -> str:
        """``inactive`` | ``ok`` | ``violated`` against the hard requirements q^H."""
        series = self.flows.get(dep_id)
        if not self.subnet.get("version") or not series:
            return "inactive"
        _ts, rx, owd, loss = series[-1]
        dep = next(d for d in self.task.dag.dependencies if d.dep_id == dep_id)
        req = self.task.requirements_for(dep)
        ok = (rx >= req.min_throughput_mbps and owd is not None and owd <= req.max_delay_ms
              and loss is not None and loss <= req.max_loss_rate)
        return "ok" if ok else "violated"

    def snapshot(self, now: float) -> dict:
        return {
            "type": "snapshot",
            "scenario": self.scenario_info(),
            "subnet": self.subnet,
            "logs": list(self.logs),
            "flows": {dep: list(series) for dep, series in self.flows.items()},
            "access": {gw: list(series) for gw, series in self.access.items()},
            "agent_logs": {layer: list(items) for layer, items in self.agent_logs.items()},
            "faults": list(self.faults),
            "status": self.status(now),
        }

    def scenario_info(self) -> dict:
        task: TaskSpecification = self.task
        req = task.hard
        return {
            "id": self.scenario,
            "gateways": list(self.world.graph.gateways),
            "links": [
                {"id": link.link_id, "src": link.source_gateway, "dst": link.target_gateway,
                 "capacity": link.capacity_mbps, "delay": link.delay_ms}
                for link in self.world.graph.links
            ],
            "endpoints": [
                {"id": ep.agent_id, "name": ep.name, "gateway": ep.gateway_id}
                for ep in task.dag.endpoints
            ],
            "deps": [
                {"id": dep.dep_id, "src": dep.source, "dst": dep.target, "demand": dep.demand_mbps}
                for dep in task.dag.dependencies
            ],
            "qos": {"min_mbps": req.min_throughput_mbps, "max_delay_ms": req.max_delay_ms,
                    "max_loss": req.max_loss_rate},
            "agents": self.agent_ids,
        }
