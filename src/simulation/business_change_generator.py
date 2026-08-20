from __future__ import annotations

import random
from dataclasses import dataclass, replace
from enum import Enum

from experiments.paper_protocol import stable_fingerprint
from src.core.models import (
    AgentLayer,
    AgentRole,
    BusinessEdge,
    TaskSpec,
    to_jsonable,
)
from src.sim.topology import AgentSpec, TopologyCatalog
from src.simulation.paper_scenarios import (
    FormationScenarioSnapshot,
    generate_formation_snapshot,
)


class BusinessChangeType(str, Enum):
    AGENT_ADD = "agent_add"
    AGENT_REMOVE = "agent_remove"
    DAG_EDGE_CHANGE = "dag_edge_change"
    QOS_UPDATE = "qos_update"


@dataclass(frozen=True)
class BusinessChange:
    change_id: str
    change_type: BusinessChangeType
    anchor_agent_ids: tuple[str, ...]
    seed_edge_ids: tuple[str, ...]
    changed_object_ids: tuple[str, ...]
    dependency_radius: int
    payload: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DependencyClosure:
    edge_ids: frozenset[str]
    agent_ids: frozenset[str]

    @property
    def fingerprint(self) -> str:
        return stable_fingerprint(
            {
                "edge_ids": sorted(self.edge_ids),
                "agent_ids": sorted(self.agent_ids),
            }
        )


@dataclass(frozen=True)
class BusinessChangeSnapshot:
    seed: int
    event_id: int
    target_bucket_percent: int | None
    generation_attempts: int
    formation_snapshot: FormationScenarioSnapshot
    before_task: TaskSpec
    after_task: TaskSpec
    change: BusinessChange
    closure: DependencyClosure

    @property
    def affected_edge_ids(self) -> frozenset[str]:
        return self.closure.edge_ids

    @property
    def affected_scope_ratio(self) -> float:
        return len(self.affected_edge_ids) / len(self.before_task.biz_edges)

    @property
    def event_fingerprint(self) -> str:
        return stable_fingerprint(
            {
                "seed": self.seed,
                "event_id": self.event_id,
                "change": self.change,
                "after_task": to_jsonable(self.after_task),
            }
        )

    @property
    def fingerprint(self) -> str:
        return stable_fingerprint(
            {
                "seed": self.seed,
                "event_id": self.event_id,
                "target_bucket_percent": self.target_bucket_percent,
                "formation_snapshot": self.formation_snapshot.fingerprint,
                "before_task": to_jsonable(self.before_task),
                "after_task": to_jsonable(self.after_task),
                "change": self.change,
                "closure": self.closure,
            }
        )

    def instantiate(self):
        return self.formation_snapshot.instantiate()


def generate_business_change(
    seed: int,
    event_id: int,
    *,
    attempt: int = 0,
    schedule_index: int | None = None,
) -> BusinessChangeSnapshot:
    if event_id < 0 or attempt < 0:
        raise ValueError("event_id and attempt must be non-negative")
    base = generate_formation_snapshot(24, seed=seed, event_id=event_id)
    type_index = event_id if schedule_index is None else schedule_index
    change_type = tuple(BusinessChangeType)[type_index % len(BusinessChangeType)]
    rng = random.Random(
        int(
            stable_fingerprint(
                {
                    "experiment": "exp3",
                    "seed": seed,
                    "event_id": event_id,
                    "schedule_index": type_index,
                    "attempt": attempt,
                }
            )[:16],
            16,
        )
    )
    dependency_radius = rng.randrange(0, 4)
    formation_snapshot = base
    if change_type == BusinessChangeType.AGENT_ADD:
        formation_snapshot, after_task, change = _agent_add_change(
            base,
            rng,
            seed,
            event_id,
            attempt,
            dependency_radius,
        )
    elif change_type == BusinessChangeType.AGENT_REMOVE:
        after_task, change = _agent_remove_change(
            base.task,
            rng,
            seed,
            event_id,
            attempt,
            dependency_radius,
        )
    elif change_type == BusinessChangeType.DAG_EDGE_CHANGE:
        after_task, change = _dag_edge_change(
            base.task,
            rng,
            seed,
            event_id,
            attempt,
            dependency_radius,
        )
    else:
        after_task, change = _qos_update_change(
            base.task,
            rng,
            seed,
            event_id,
            attempt,
            dependency_radius,
        )
    closure = exact_dependency_closure(base.task, change)
    after_task = _materialize_dependency_effect(
        base.task,
        after_task,
        closure,
    )
    return BusinessChangeSnapshot(
        seed=seed,
        event_id=event_id,
        target_bucket_percent=None,
        generation_attempts=1,
        formation_snapshot=formation_snapshot,
        before_task=base.task,
        after_task=after_task,
        change=change,
        closure=closure,
    )


def exact_dependency_closure(
    before_task: TaskSpec,
    change: BusinessChange,
) -> DependencyClosure:
    """Compute the modular task dependency neighborhood for a materialized change."""

    edge_by_id = {edge.edge_id: edge for edge in before_task.biz_edges}
    incident: dict[str, list[BusinessEdge]] = {
        agent_id: [] for agent_id in before_task.app_agents
    }
    affected_edges = {
        edge_id for edge_id in change.seed_edge_ids if edge_id in edge_by_id
    }
    affected_agents = set(change.anchor_agent_ids)
    for edge in before_task.biz_edges:
        incident.setdefault(edge.source, []).append(edge)
        incident.setdefault(edge.target, []).append(edge)
    for edge_id in affected_edges:
        edge = edge_by_id[edge_id]
        affected_agents.update((edge.source, edge.target))

    frontier = set(change.anchor_agent_ids)
    visited: set[str] = set()
    for _depth in range(change.dependency_radius + 1):
        next_frontier: set[str] = set()
        for agent_id in sorted(frontier):
            visited.add(agent_id)
            for edge in incident.get(agent_id, ()):
                affected_edges.add(edge.edge_id)
                affected_agents.update((edge.source, edge.target))
                next_frontier.update((edge.source, edge.target))
        frontier = next_frontier - visited
        if not frontier:
            break
    if not affected_edges:
        raise ValueError(f"business change has an empty dependency closure: {change.change_id}")
    return DependencyClosure(
        edge_ids=frozenset(affected_edges),
        agent_ids=frozenset(affected_agents),
    )


def sample_affected_scope_bucket(
    bucket_percent: int,
    seed: int,
    event_id: int,
    *,
    schedule_index: int | None = None,
    tolerance_percent: float = 5.0,
    max_attempts: int = 2048,
) -> BusinessChangeSnapshot:
    if bucket_percent not in {10, 20, 30, 40, 50}:
        raise ValueError("affected-scope bucket must be one of 10, 20, 30, 40, 50")
    if tolerance_percent < 0.0 or max_attempts <= 0:
        raise ValueError("invalid affected-scope sampler limits")
    observed: list[float] = []
    for attempt in range(max_attempts):
        candidate = generate_business_change(
            seed,
            event_id,
            attempt=attempt,
            schedule_index=schedule_index,
        )
        observed_percent = 100.0 * candidate.affected_scope_ratio
        observed.append(observed_percent)
        if abs(observed_percent - bucket_percent) <= tolerance_percent + 1e-9:
            return replace(
                candidate,
                target_bucket_percent=bucket_percent,
                generation_attempts=attempt + 1,
            )
    closest = sorted(observed, key=lambda value: abs(value - bucket_percent))[:8]
    raise RuntimeError(
        f"unable to sample affected-scope bucket {bucket_percent}% after "
        f"{max_attempts} generated changes; closest observed={closest}"
    )


def _agent_add_change(
    base: FormationScenarioSnapshot,
    rng: random.Random,
    seed: int,
    event_id: int,
    attempt: int,
    dependency_radius: int,
) -> tuple[FormationScenarioSnapshot, TaskSpec, BusinessChange]:
    pivot = rng.choice(base.task.biz_edges)
    new_agent_id = f"agent-added-{seed:04d}-{event_id:03d}-{attempt:04d}"
    gateway_id = base.agent_gateway_mapping[pivot.source]
    gateway_index = base.topology.gateway_ids.index(gateway_id)
    extra_spec = AgentSpec(
        agent_id=new_agent_id,
        name="Paper Added Business Agent",
        layer=AgentLayer.APPLICATION,
        role=AgentRole.BUSINESS,
        gateway_id=gateway_id,
        capabilities=("business_stage",),
        port=16000 + (attempt % 1000),
    )
    catalog = TopologyCatalog(
        gateways=base.catalog.gateways,
        agents=base.catalog.agents + (extra_spec,),
    )
    formation_snapshot = replace(
        base,
        catalog=catalog,
        agent_gateway_mapping={
            **base.agent_gateway_mapping,
            new_agent_id: gateway_id,
        },
    )
    first = replace(
        pivot,
        target=new_agent_id,
        flow_type=f"agent-add-ingress-{attempt}",
        edge_id=f"edge-add-in-{seed:04d}-{event_id:03d}-{attempt:04d}",
        latency_budget_ms=max(30.0, pivot.latency_budget_ms * 0.55),
    )
    second = replace(
        pivot,
        source=new_agent_id,
        flow_type=f"agent-add-egress-{attempt}",
        edge_id=f"edge-add-out-{seed:04d}-{event_id:03d}-{attempt:04d}",
    )
    after_edges = tuple(
        edge for edge in base.task.biz_edges if edge.edge_id != pivot.edge_id
    ) + (first, second)
    after_task = replace(
        base.task,
        app_agents=base.task.app_agents + (new_agent_id,),
        biz_edges=after_edges,
    )
    change = BusinessChange(
        change_id=_change_id(seed, event_id, attempt),
        change_type=BusinessChangeType.AGENT_ADD,
        anchor_agent_ids=(pivot.source,),
        seed_edge_ids=(pivot.edge_id,),
        changed_object_ids=(new_agent_id, pivot.edge_id, first.edge_id, second.edge_id),
        dependency_radius=dependency_radius,
        payload=(("gateway_id", gateway_id), ("gateway_index", str(gateway_index))),
    )
    return formation_snapshot, after_task, change


def _agent_remove_change(
    task: TaskSpec,
    rng: random.Random,
    seed: int,
    event_id: int,
    attempt: int,
    dependency_radius: int,
) -> tuple[TaskSpec, BusinessChange]:
    incident_counts = {
        agent_id: sum(
            edge.source == agent_id or edge.target == agent_id
            for edge in task.biz_edges
        )
        for agent_id in task.app_agents
    }
    candidates = [agent_id for agent_id, count in incident_counts.items() if count >= 2]
    removed = rng.choice(candidates)
    incident_ids = tuple(
        edge.edge_id
        for edge in task.biz_edges
        if edge.source == removed or edge.target == removed
    )
    after_task = replace(
        task,
        app_agents=tuple(agent_id for agent_id in task.app_agents if agent_id != removed),
        biz_edges=tuple(
            edge
            for edge in task.biz_edges
            if edge.source != removed and edge.target != removed
        ),
    )
    return after_task, BusinessChange(
        change_id=_change_id(seed, event_id, attempt),
        change_type=BusinessChangeType.AGENT_REMOVE,
        anchor_agent_ids=(removed,),
        seed_edge_ids=incident_ids,
        changed_object_ids=(removed, *incident_ids),
        dependency_radius=dependency_radius,
    )


def _dag_edge_change(
    task: TaskSpec,
    rng: random.Random,
    seed: int,
    event_id: int,
    attempt: int,
    dependency_radius: int,
) -> tuple[TaskSpec, BusinessChange]:
    old = rng.choice(task.biz_edges)
    positions = {agent_id: index for index, agent_id in enumerate(task.app_agents)}
    existing = {(edge.source, edge.target) for edge in task.biz_edges}
    candidates = [
        (source, target)
        for source in task.app_agents
        for target in task.app_agents
        if positions[source] < positions[target]
        and (source, target) not in existing
        and source != old.source
    ]
    source, target = rng.choice(candidates)
    replacement = replace(
        old,
        source=source,
        target=target,
        flow_type=f"dag-edge-change-{attempt}",
        edge_id=f"edge-change-{seed:04d}-{event_id:03d}-{attempt:04d}",
    )
    after_task = replace(
        task,
        biz_edges=tuple(edge for edge in task.biz_edges if edge.edge_id != old.edge_id)
        + (replacement,),
    )
    return after_task, BusinessChange(
        change_id=_change_id(seed, event_id, attempt),
        change_type=BusinessChangeType.DAG_EDGE_CHANGE,
        anchor_agent_ids=(old.source,),
        seed_edge_ids=(old.edge_id,),
        changed_object_ids=(old.edge_id, replacement.edge_id),
        dependency_radius=dependency_radius,
        payload=(("new_source", source), ("new_target", target)),
    )


def _qos_update_change(
    task: TaskSpec,
    rng: random.Random,
    seed: int,
    event_id: int,
    attempt: int,
    dependency_radius: int,
) -> tuple[TaskSpec, BusinessChange]:
    selected = rng.choice(task.biz_edges)
    updated = replace(
        selected,
        data_rate_mbps=round(selected.data_rate_mbps * 1.10, 6),
        priority=min(3, selected.priority + 1),
    )
    after_task = replace(
        task,
        biz_edges=tuple(
            updated if edge.edge_id == selected.edge_id else edge
            for edge in task.biz_edges
        ),
    )
    return after_task, BusinessChange(
        change_id=_change_id(seed, event_id, attempt),
        change_type=BusinessChangeType.QOS_UPDATE,
        anchor_agent_ids=(selected.source,),
        seed_edge_ids=(selected.edge_id,),
        changed_object_ids=(selected.edge_id,),
        dependency_radius=dependency_radius,
        payload=(("data_rate_multiplier", "1.10"),),
    )


def _change_id(seed: int, event_id: int, attempt: int) -> str:
    return f"business-change-{seed:04d}-{event_id:03d}-{attempt:04d}"


def _materialize_dependency_effect(
    before_task: TaskSpec,
    after_task: TaskSpec,
    closure: DependencyClosure,
) -> TaskSpec:
    """Apply the generated change's propagated policy effect to surviving edges."""

    before = {edge.edge_id: edge for edge in before_task.biz_edges}
    updated_edges = []
    for edge in after_task.biz_edges:
        previous = before.get(edge.edge_id)
        if previous is None or edge.edge_id not in closure.edge_ids:
            updated_edges.append(edge)
            continue
        updated_edges.append(
            replace(
                edge,
                priority=1 + (previous.priority % 3),
            )
        )
    return replace(after_task, biz_edges=tuple(updated_edges))
