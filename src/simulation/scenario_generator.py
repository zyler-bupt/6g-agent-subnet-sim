from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from typing import Any

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


@dataclass(frozen=True)
class ScenarioConfig:
    num_agents: int = 20
    edge_ratio: float = 1.5
    num_gateways: int = 4
    cross_gateway_edge_ratio: float = 0.5
    agent_removal_ratio: float = 0.1
    removed_agent_type: str = "leaf"
    dag_type: str = "random"
    multi_hop: bool = True

    def validate(self) -> None:
        if self.num_agents < 8:
            raise ValueError("num_agents must be at least 8")
        if self.num_gateways < 1:
            raise ValueError("num_gateways must be positive")
        if self.edge_ratio <= 0.0:
            raise ValueError("edge_ratio must be positive")
        if not 0.0 <= self.cross_gateway_edge_ratio <= 1.0:
            raise ValueError("cross_gateway_edge_ratio must be in [0, 1]")
        if not 0.0 < self.agent_removal_ratio < 1.0:
            raise ValueError("agent_removal_ratio must be in (0, 1)")
        if self.removed_agent_type not in {"leaf", "intermediate", "fan_in", "fan_out"}:
            raise ValueError(f"unsupported removed_agent_type: {self.removed_agent_type}")
        if self.dag_type != "random":
            raise ValueError("only acyclic random DAG generation is implemented")


@dataclass(frozen=True)
class ScenarioSnapshot:
    seed: int
    config: ScenarioConfig
    task: TaskSpec
    catalog: TopologyCatalog
    gateway_paths: dict[tuple[str, str], tuple[str, ...]]
    agent_types: dict[str, str]
    removed_agent_ids: tuple[str, ...]
    actual_cross_gateway_edge_ratio: float

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "config": asdict(self.config),
            "task": to_jsonable(self.task),
            "catalog": to_jsonable(self.catalog),
            "gateway_paths": {
                f"{source}->{target}": list(path)
                for (source, target), path in sorted(self.gateway_paths.items())
            },
            "agent_types": dict(sorted(self.agent_types.items())),
            "removed_agent_ids": list(self.removed_agent_ids),
            "actual_cross_gateway_edge_ratio": self.actual_cross_gateway_edge_ratio,
        }

    def instantiate(
        self,
    ) -> tuple[AgentController, SyntheticTaskSubnetVerifier, SyntheticMetricProvider]:
        provider = SyntheticMetricProvider(
            seed=self.seed,
            base_bandwidth_mbps=250.0,
            base_latency_ms=8.0,
            base_app_rate_mbps=20.0,
        )
        controller = AgentController(
            build_topology_from_catalog(self.catalog, provider),
            gateway_paths=self.gateway_paths,
        )
        verifier = SyntheticTaskSubnetVerifier(provider, probe_delay_ms=0.0)
        return controller, verifier, provider


class ElasticScenarioGenerator:
    """Create one immutable scenario snapshot reused by every method."""

    def generate(self, config: ScenarioConfig, seed: int) -> ScenarioSnapshot:
        config.validate()
        rng = random.Random(seed)
        gateways = tuple(f"gw-{index:02d}" for index in range(config.num_gateways))
        gateway_specs = tuple(
            GatewaySpec(
                gateway_id=gateway_id,
                subnet_id=f"subnet-{index:02d}",
                node=f"node-{index:02d}",
                gateway_ip=f"10.60.{index + 1}.1",
            )
            for index, gateway_id in enumerate(gateways)
        )
        agent_ids = tuple(f"agent-{index:03d}" for index in range(config.num_agents))
        deployment = {
            agent_id: gateways[index % len(gateways)]
            for index, agent_id in enumerate(agent_ids)
        }
        # Seed changes deployment while keeping every gateway populated.
        shuffled_gateways = list(gateways)
        rng.shuffle(shuffled_gateways)
        deployment = {
            agent_id: shuffled_gateways[index % len(shuffled_gateways)]
            for index, agent_id in enumerate(agent_ids)
        }
        app_specs = tuple(
            AgentSpec(
                agent_id=agent_id,
                name=f"Business Agent {index}",
                layer=AgentLayer.APPLICATION,
                role=AgentRole.BUSINESS,
                gateway_id=deployment[agent_id],
                capabilities=("business_stage",),
                port=9000 + index,
            )
            for index, agent_id in enumerate(agent_ids)
        )
        support_specs = tuple(
            replace_physical_capacity(spec, 10000.0)
            for gateway_id in gateways
            for spec in support_agent_specs(gateway_id)
        )
        catalog = TopologyCatalog(
            gateways=gateway_specs,
            agents=app_specs + support_specs,
        )

        edge_indices = _generate_edge_indices(
            config,
            deployment,
            agent_ids,
            rng,
        )
        edges = tuple(
            BusinessEdge(
                source=agent_ids[source],
                target=agent_ids[target],
                flow_type=f"data-{index:04d}",
                data_rate_mbps=1.0 + (index % 3) * 0.25,
                latency_budget_ms=120.0,
                priority=1 + index % 3,
                edge_id=f"edge-{index:04d}",
                max_loss_rate=0.02,
                min_reliability=0.98,
            )
            for index, (source, target) in enumerate(sorted(edge_indices))
        )
        task = TaskSpec(
            task_id=f"exp3-seed-{seed:04d}",
            goal="business-change-driven elastic Agent removal",
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
        agent_types = classify_agent_types(task)
        candidates = [
            agent_id
            for agent_id in agent_ids
            if agent_types.get(agent_id) == config.removed_agent_type
        ]
        if not candidates:
            raise ValueError(
                f"generated DAG has no {config.removed_agent_type} Agent"
            )
        removal_count = max(1, math.ceil(config.agent_removal_ratio * config.num_agents))
        primary = rng.choice(candidates)
        remaining = [agent_id for agent_id in agent_ids if agent_id != primary]
        rng.shuffle(remaining)
        removed = (primary, *remaining[: max(0, removal_count - 1)])
        cross_edges = sum(
            deployment[edge.source] != deployment[edge.target] for edge in edges
        )
        return ScenarioSnapshot(
            seed=seed,
            config=config,
            task=task,
            catalog=catalog,
            gateway_paths=_gateway_paths(gateways, config.multi_hop),
            agent_types=agent_types,
            removed_agent_ids=tuple(removed),
            actual_cross_gateway_edge_ratio=(cross_edges / len(edges) if edges else 0.0),
        )


def classify_agent_types(task: TaskSpec) -> dict[str, str]:
    indegree = {agent_id: 0 for agent_id in task.app_agents}
    outdegree = {agent_id: 0 for agent_id in task.app_agents}
    for edge in task.biz_edges:
        outdegree[edge.source] += 1
        indegree[edge.target] += 1
    result: dict[str, str] = {}
    for agent_id in task.app_agents:
        if outdegree[agent_id] >= 2:
            result[agent_id] = "fan_out"
        elif indegree[agent_id] >= 2:
            result[agent_id] = "fan_in"
        elif indegree[agent_id] >= 1 and outdegree[agent_id] >= 1:
            result[agent_id] = "intermediate"
        elif outdegree[agent_id] == 0 and indegree[agent_id] == 1:
            result[agent_id] = "leaf"
        else:
            result[agent_id] = "endpoint"
    return result


def replace_physical_capacity(spec: AgentSpec, capacity_mbps: float) -> AgentSpec:
    if spec.layer != AgentLayer.PHYSICAL:
        return spec
    values = dict(spec.state_values)
    values["total_capacity_mbps"] = capacity_mbps
    return AgentSpec(
        agent_id=spec.agent_id,
        name=spec.name,
        layer=spec.layer,
        role=spec.role,
        gateway_id=spec.gateway_id,
        capabilities=spec.capabilities,
        subnet_id=spec.subnet_id,
        node=spec.node,
        endpoint=spec.endpoint,
        ip=spec.ip,
        port=spec.port,
        status=spec.status,
        state_values=values,
    )


def _generate_edge_indices(
    config: ScenarioConfig,
    deployment: dict[str, str],
    agent_ids: tuple[str, ...],
    rng: random.Random,
) -> set[tuple[int, int]]:
    # Reserved motifs guarantee all four requested removal positions.  Their
    # incident nodes are kept out of random additions so classifications remain
    # stable and reproducible.
    edges: set[tuple[int, int]] = {
        (0, 1),
        (0, 2),  # agent 0: fan-out
        (1, 3),  # agent 1: intermediate
        (2, 4),
        (3, 5),
        (4, 5),  # agent 5: fan-in
        (6, 7),  # agent 7: leaf
    }
    protected = {0, 1, 5, 7}
    candidates = [
        (source, target)
        for source in range(config.num_agents)
        for target in range(source + 1, config.num_agents)
        if (source, target) not in edges
        and source not in protected
        and target not in protected
    ]
    rng.shuffle(candidates)
    target_count = max(len(edges), min(len(candidates) + len(edges), round(
        config.edge_ratio * config.num_agents
    )))
    desired_cross = round(target_count * config.cross_gateway_edge_ratio)
    current_cross = sum(
        deployment[agent_ids[source]] != deployment[agent_ids[target]]
        for source, target in edges
    )
    cross = [
        item
        for item in candidates
        if deployment[agent_ids[item[0]]] != deployment[agent_ids[item[1]]]
    ]
    cross_set = set(cross)
    local = [item for item in candidates if item not in cross_set]
    rng.shuffle(cross)
    rng.shuffle(local)
    need = target_count - len(edges)
    cross_need = min(len(cross), max(0, desired_cross - current_cross), need)
    selected = cross[:cross_need]
    local_need = min(len(local), need - len(selected))
    selected.extend(local[:local_need])
    if len(selected) < need:
        used = set(selected)
        selected.extend(
            item for item in candidates if item not in used
        )
    edges.update(selected[:need])
    return edges


def _gateway_paths(
    gateways: tuple[str, ...],
    multi_hop: bool,
) -> dict[tuple[str, str], tuple[str, ...]]:
    paths: dict[tuple[str, str], tuple[str, ...]] = {}
    for source_index, source in enumerate(gateways):
        for target_index, target in enumerate(gateways):
            if source == target:
                paths[(source, target)] = (source,)
            elif multi_hop:
                step = 1 if target_index > source_index else -1
                paths[(source, target)] = tuple(
                    gateways[index]
                    for index in range(source_index, target_index + step, step)
                )
            else:
                paths[(source, target)] = (source, target)
    return paths
