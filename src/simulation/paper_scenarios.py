from __future__ import annotations

import heapq
import random
from dataclasses import asdict, dataclass
from typing import Any

from experiments.paper_protocol import stable_fingerprint
from src.controller.networking import AgentController
from src.core.models import (
    AgentLayer,
    AgentRole,
    BusinessEdge,
    QoSRequirements,
    TaskSpec,
    to_jsonable,
)
from src.e2e.verifiers import SyntheticTaskSubnetVerifier
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.topology import (
    AgentSpec,
    GatewaySpec,
    TopologyCatalog,
    build_topology_from_catalog,
    support_agent_specs,
)
from src.simulation.scenario_generator import replace_physical_capacity


@dataclass(frozen=True)
class PaperLink:
    link_id: str
    source: str
    target: str
    delay_ms: float
    bandwidth_mbps: float
    loss_percent: float
    jitter_ms: float


@dataclass(frozen=True)
class PaperTopology:
    gateway_ids: tuple[str, ...]
    links: tuple[PaperLink, ...]

    @property
    def fingerprint(self) -> str:
        return stable_fingerprint(
            {
                "gateway_ids": self.gateway_ids,
                "links": [asdict(link) for link in self.links],
            }
        )

    @property
    def connected(self) -> bool:
        if not self.gateway_ids:
            return False
        adjacency = self._adjacency()
        visited = {self.gateway_ids[0]}
        pending = [self.gateway_ids[0]]
        while pending:
            current = pending.pop()
            for neighbor, _delay in adjacency[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        return visited == set(self.gateway_ids)

    @property
    def gateway_paths(self) -> dict[tuple[str, str], tuple[str, ...]]:
        return {
            (source, target): self.shortest_path(source, target)
            for source in self.gateway_ids
            for target in self.gateway_ids
        }

    def degrees(self) -> dict[str, int]:
        values = {gateway_id: 0 for gateway_id in self.gateway_ids}
        for link in self.links:
            values[link.source] += 1
            values[link.target] += 1
        return values

    def shortest_path(self, source: str, target: str) -> tuple[str, ...]:
        if source not in self.gateway_ids or target not in self.gateway_ids:
            raise KeyError(f"unknown Gateway path endpoint: {source}->{target}")
        if source == target:
            return (source,)
        adjacency = self._adjacency()
        pending: list[tuple[float, tuple[str, ...], str]] = [(0.0, (source,), source)]
        best = {source: 0.0}
        while pending:
            cost, path, current = heapq.heappop(pending)
            if current == target:
                return path
            if cost > best.get(current, float("inf")):
                continue
            for neighbor, delay_ms in adjacency[current]:
                candidate = cost + delay_ms
                if candidate < best.get(neighbor, float("inf")):
                    best[neighbor] = candidate
                    heapq.heappush(pending, (candidate, path + (neighbor,), neighbor))
        raise ValueError(f"mesh is disconnected: {source}->{target}")

    def _adjacency(self) -> dict[str, list[tuple[str, float]]]:
        adjacency = {gateway_id: [] for gateway_id in self.gateway_ids}
        for link in self.links:
            adjacency[link.source].append((link.target, link.delay_ms))
            adjacency[link.target].append((link.source, link.delay_ms))
        for neighbors in adjacency.values():
            neighbors.sort()
        return adjacency


@dataclass(frozen=True)
class FormationScenarioSnapshot:
    seed: int
    event_id: int
    topology: PaperTopology
    task: TaskSpec
    catalog: TopologyCatalog
    agent_gateway_mapping: dict[str, str]
    cross_gateway_edge_ratio: float
    event_fingerprint: str

    @property
    def fingerprint(self) -> str:
        return stable_fingerprint(
            {
                "seed": self.seed,
                "topology": self.topology,
                "task": to_jsonable(self.task),
                "catalog": to_jsonable(self.catalog),
                "agent_gateway_mapping": self.agent_gateway_mapping,
                "cross_gateway_edge_ratio": self.cross_gateway_edge_ratio,
            }
        )

    @property
    def qos_fingerprint(self) -> str:
        return stable_fingerprint(self.task.qos)

    def instantiate(
        self,
    ) -> tuple[AgentController, SyntheticTaskSubnetVerifier, SyntheticMetricProvider]:
        provider = SyntheticMetricProvider(
            seed=self.seed * 1000 + self.event_id,
            base_bandwidth_mbps=250.0,
            base_latency_ms=8.0,
            base_app_rate_mbps=20.0,
        )
        controller = AgentController(
            build_topology_from_catalog(self.catalog, provider),
            gateway_paths=self.topology.gateway_paths,
        )
        verifier = SyntheticTaskSubnetVerifier(provider, probe_delay_ms=0.0)
        return controller, verifier, provider

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "event_id": self.event_id,
            "topology": {
                "gateway_ids": list(self.topology.gateway_ids),
                "links": [asdict(link) for link in self.topology.links],
            },
            "task": to_jsonable(self.task),
            "catalog": to_jsonable(self.catalog),
            "agent_gateway_mapping": dict(sorted(self.agent_gateway_mapping.items())),
            "cross_gateway_edge_ratio": self.cross_gateway_edge_ratio,
            "event_fingerprint": self.event_fingerprint,
        }


def generate_mesh_topology(seed: int, num_gateways: int = 12) -> PaperTopology:
    if num_gateways < 4:
        raise ValueError("paper mesh requires at least four Gateways")
    rng = random.Random(_derived_seed("mesh", seed, num_gateways))
    gateway_ids = tuple(f"gw-{index:02d}" for index in range(num_gateways))
    edge_pairs = {
        tuple(sorted((gateway_ids[index], gateway_ids[(index + 1) % num_gateways])))
        for index in range(num_gateways)
    }
    degrees = {gateway_id: 2 for gateway_id in gateway_ids}
    candidates = [
        (gateway_ids[left], gateway_ids[right])
        for left in range(num_gateways)
        for right in range(left + 1, num_gateways)
        if (gateway_ids[left], gateway_ids[right]) not in edge_pairs
    ]
    rng.shuffle(candidates)
    target_edges = round(1.75 * num_gateways)
    for source, target in candidates:
        if len(edge_pairs) >= target_edges:
            break
        if degrees[source] >= 5 or degrees[target] >= 5:
            continue
        edge_pairs.add((source, target))
        degrees[source] += 1
        degrees[target] += 1
    if len(edge_pairs) != target_edges:
        raise RuntimeError("unable to construct degree-bounded paper mesh")

    links = []
    for index, (source, target) in enumerate(sorted(edge_pairs)):
        links.append(
            PaperLink(
                link_id=f"link-{index:03d}-{source}-{target}",
                source=source,
                target=target,
                delay_ms=round(rng.uniform(5.0, 30.0), 6),
                bandwidth_mbps=round(rng.uniform(50.0, 200.0), 6),
                loss_percent=round(rng.uniform(0.0, 1.0), 6),
                jitter_ms=round(rng.uniform(0.0, 5.0), 6),
            )
        )
    return PaperTopology(gateway_ids=gateway_ids, links=tuple(links))


def generate_formation_snapshot(
    task_size: int,
    seed: int,
    event_id: int,
) -> FormationScenarioSnapshot:
    if task_size < 8 or task_size % 4:
        raise ValueError("paper formation task size must be a multiple of four and at least eight")
    if event_id < 0:
        raise ValueError("event_id must be non-negative")
    topology = generate_mesh_topology(seed=seed, num_gateways=12)
    agent_ids = tuple(f"agent-{index:03d}" for index in range(task_size))
    edge_indices = _modular_edge_indices(task_size, seed)
    mapping, cross_ratio = _placement_for_cross_gateway_target(
        agent_ids,
        edge_indices,
        topology.gateway_ids,
        seed,
    )
    gateway_specs = tuple(
        GatewaySpec(
            gateway_id=gateway_id,
            subnet_id=f"subnet-{index:02d}",
            node=f"node-{index:02d}",
            gateway_ip=f"10.70.{index + 1}.1",
        )
        for index, gateway_id in enumerate(topology.gateway_ids)
    )
    app_specs = tuple(
        AgentSpec(
            agent_id=agent_id,
            name=f"Paper Business Agent {index}",
            layer=AgentLayer.APPLICATION,
            role=AgentRole.BUSINESS,
            gateway_id=mapping[agent_id],
            capabilities=("business_stage",),
            port=10000 + index,
        )
        for index, agent_id in enumerate(agent_ids)
    )
    support_specs = tuple(
        replace_physical_capacity(spec, 10000.0)
        for gateway_id in topology.gateway_ids
        for spec in support_agent_specs(gateway_id)
    )
    catalog = TopologyCatalog(
        gateways=gateway_specs,
        agents=app_specs + support_specs,
    )
    edges = tuple(
        BusinessEdge(
            source=agent_ids[source],
            target=agent_ids[target],
            flow_type=f"task-flow-{index:04d}",
            data_rate_mbps=5.0 + float(index % 5),
            latency_budget_ms=120.0,
            priority=1 + index % 3,
            edge_id=f"edge-{index:04d}",
            max_loss_rate=0.02,
            min_reliability=0.98,
        )
        for index, (source, target) in enumerate(sorted(edge_indices))
    )
    task = TaskSpec(
        task_id=f"paper-exp1-size-{task_size:02d}-seed-{seed:04d}",
        goal="form a modular fork-join task communication subnet",
        app_agents=agent_ids,
        biz_edges=edges,
        qos=QoSRequirements(
            max_latency_ms=150.0,
            max_loss_rate=0.03,
            min_reliability=0.98,
            min_bandwidth_mbps=10.0,
            data_volume_mb=100.0,
            priority=2,
        ),
    )
    return FormationScenarioSnapshot(
        seed=seed,
        event_id=event_id,
        topology=topology,
        task=task,
        catalog=catalog,
        agent_gateway_mapping=mapping,
        cross_gateway_edge_ratio=cross_ratio,
        event_fingerprint=stable_fingerprint(
            {
                "experiment": "exp1",
                "seed": seed,
                "event_id": event_id,
                "event_type": "formation",
            }
        ),
    )


def _modular_edge_indices(task_size: int, seed: int) -> set[tuple[int, int]]:
    edges: set[tuple[int, int]] = set()
    split_nodes = tuple(range(0, task_size, 4))
    for split in split_nodes:
        edges.update(
            {
                (split, split + 1),
                (split, split + 2),
                (split + 1, split + 3),
                (split + 2, split + 3),
            }
        )
        if split + 4 < task_size:
            edges.add((split + 3, split + 4))

    target_count = round(1.6 * task_size)
    candidates = [
        (source, target)
        for source in split_nodes
        for target in range(source + 1, min(task_size, source + 8))
        if (source, target) not in edges
    ]
    rng = random.Random(_derived_seed("dag", seed, task_size))
    rng.shuffle(candidates)
    for edge in candidates:
        if len(edges) >= target_count:
            break
        edges.add(edge)
    if len(edges) != target_count:
        raise RuntimeError("unable to reach target modular DAG density")
    return edges


def _placement_for_cross_gateway_target(
    agent_ids: tuple[str, ...],
    edges: set[tuple[int, int]],
    gateway_ids: tuple[str, ...],
    seed: int,
) -> tuple[dict[str, str], float]:
    rng = random.Random(_derived_seed("placement", seed, len(agent_ids)))
    active_count = min(len(gateway_ids), max(3, len(agent_ids) // 3))
    active_gateways = list(gateway_ids)
    rng.shuffle(active_gateways)
    active_gateways = active_gateways[:active_count]
    best_mapping: dict[str, str] | None = None
    best_ratio = 0.0
    best_score = (float("inf"), float("inf"), 0)

    for attempt in range(6000):
        stickiness = (attempt % 81) / 100.0
        placements = [rng.choice(active_gateways)]
        for _agent_id in agent_ids[1:]:
            if rng.random() < stickiness:
                placements.append(placements[-1])
            else:
                placements.append(rng.choice(active_gateways))
        cross = sum(placements[source] != placements[target] for source, target in edges)
        ratio = cross / len(edges)
        outside = max(0.60 - ratio, ratio - 0.70, 0.0)
        used = len(set(placements))
        score = (outside, abs(ratio - 0.65), -used)
        if score < best_score:
            best_score = score
            best_ratio = ratio
            best_mapping = dict(zip(agent_ids, placements))
            if outside == 0.0 and abs(ratio - 0.65) <= 0.01 and used >= min(3, active_count):
                break

    if best_mapping is None or not 0.60 <= best_ratio <= 0.70:
        raise RuntimeError(
            f"unable to place Agents at 60-70% cross-Gateway edges: {best_ratio:.3f}"
        )
    return best_mapping, best_ratio


def _derived_seed(namespace: str, seed: int, value: int) -> int:
    digest = stable_fingerprint(
        {"namespace": namespace, "seed": seed, "value": value}
    )
    return int(digest[:16], 16)
