"""Window data models: bus messages in, plot-ready series out (Qt-free)."""
from __future__ import annotations

from collections import defaultdict, deque

from src.tcanet.spec import TaskSpecification

_HORIZON = 600


class FlowModel:
    """Per-dependency goodput / one-way delay series plus event markers."""

    def __init__(self, dep_ids: list[str]) -> None:
        self.dep_ids = dep_ids
        self.t0: float | None = None
        self.rx: dict[str, deque] = defaultdict(lambda: deque(maxlen=_HORIZON))
        self.owd: dict[str, deque] = defaultdict(lambda: deque(maxlen=_HORIZON))
        self.markers: deque[tuple[float, str, str]] = deque(maxlen=40)
        self.source = "MEASURED"

    def _t(self, ts: float) -> float:
        if self.t0 is None:
            self.t0 = ts
        return ts - self.t0

    def apply(self, msg: dict) -> bool:
        topic, p, ts = msg["topic"], msg["payload"], float(msg["ts"])
        if topic == "report.flow" and p["dep_id"] in self.dep_ids:
            t = self._t(ts)
            self.source = p.get("source", self.source)
            self.rx[p["dep_id"]].append((t, float(p["rx_mbps"])))
            if p.get("owd_ms") is not None:
                self.owd[p["dep_id"]].append((t, float(p["owd_ms"])))
            return True
        if topic == "ctrl.log" and p["step"] in ("event", "rollback", "commit") and p["level"] != "info":
            label = {"event": "detected", "rollback": "rollback",
                     "commit": p["title"].replace("Commit ", "")}[p["step"]]
            if p["step"] == "rollback" and not p["title"].startswith("Rollback"):
                return False
            self.markers.append((self._t(ts), label, p["step"]))
            return True
        if topic == "ops.fault" and p["op"] in ("gateway", "link", "degrade", "kill", "demand"):
            target = p.get("target", p.get("dep_id", ""))
            self.markers.append((self._t(ts), f"{p['op']} {target}", "ops"))
            return True
        return False

    def series(self, dep_id: str, kind: str, gap_s: float = 1.5) -> tuple[list[float], list[float]]:
        """Points of one series; a NaN breaks the line across sampling gaps."""
        xs: list[float] = []
        ys: list[float] = []
        for t, v in (self.rx if kind == "rx" else self.owd).get(dep_id, ()):
            if xs and t - xs[-1] > gap_s:
                xs.append(xs[-1])
                ys.append(float("nan"))
            xs.append(t)
            ys.append(v)
        return xs, ys


class AccessModel:
    """PhyAgent access capacity per gateway and NetAgent link utilization."""

    def __init__(self, gateways: list[str], links: list[str]) -> None:
        self.gateways, self.links = gateways, links
        self.t0: float | None = None
        self.capacity: dict[str, deque] = defaultdict(lambda: deque(maxlen=_HORIZON))
        self.snr: dict[str, float] = {}
        self.utilization: dict[str, float] = {link: 0.0 for link in links}
        self.link_up: dict[str, bool] = {link: True for link in links}
        self._reports: dict[str, dict[str, bool]] = defaultdict(dict)

    def apply(self, msg: dict) -> bool:
        topic, p, ts = msg["topic"], msg["payload"], float(msg["ts"])
        if self.t0 is None:
            self.t0 = ts
        if topic == "report.access":
            self.capacity[p["gateway"]].append((ts - self.t0, float(p["capacity_mbps"])))
            self.snr[p["gateway"]] = float(p["snr_db"])
            return True
        if topic == "report.link" and p["link_id"] in self.link_up:
            self._reports[p["link_id"]][p["reporter"]] = bool(p["up"])
            self.link_up[p["link_id"]] = all(self._reports[p["link_id"]].values())
            if p.get("utilization") is not None:
                self.utilization[p["link_id"]] = float(p["utilization"])
            return True
        return False

    def capacity_series(self, gateway: str) -> tuple[list[float], list[float]]:
        points = self.capacity.get(gateway, ())
        return [t for t, _ in points], [v for _, v in points]


class TaskModel:
    """Task DAG state: version, paths, bindings, per-dependency q^H status."""

    def __init__(self, task: TaskSpecification) -> None:
        self.task = task
        self.version = 0
        self.paths: dict[str, list[str]] = {}
        self.bindings: dict[str, list[str | None]] = {}
        self.affected: set[str] = set()
        self.latest: dict[str, dict] = {}

    def apply(self, msg: dict) -> bool:
        topic, p = msg["topic"], msg["payload"]
        if topic == "ctrl.subnet":
            self.version = int(p["version"])
            self.paths = p["paths"]
            self.bindings = p["bindings"]
            self.affected = set()
            return True
        if topic == "ctrl.log" and p["step"] == "scope":
            self.affected = set(p.get("data", {}).get("affected", []))
            return True
        if topic == "report.flow":
            self.latest[p["dep_id"]] = p
            return True
        return False

    def status(self, dep_id: str) -> str:
        """``inactive`` | ``ok`` | ``violated`` against the hard requirements."""
        sample = self.latest.get(dep_id)
        if self.version == 0 or sample is None:
            return "inactive"
        dep = next(d for d in self.task.dag.dependencies if d.dep_id == dep_id)
        req = self.task.requirements_for(dep)
        owd = sample.get("owd_ms")
        loss = sample.get("loss")
        ok = (
            sample["rx_mbps"] >= req.min_throughput_mbps
            and owd is not None and owd <= req.max_delay_ms
            and loss is not None and loss <= req.max_loss_rate
        )
        return "ok" if ok else "violated"
