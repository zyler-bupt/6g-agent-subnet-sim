"""Versioned task-subnet state and construction (paper Sec. II-A, IV-A).

``S_m^(v_m)`` records the validated task DAG, per-edge gateway paths
``pi_{m,e}``, per-task forwarding tables ``FT^g_m``, supporting-agent
bindings ``Phi_m`` and the version index ``v_m``.

Construction follows Sec. IV-A: endpoint confirmation (replacing an
unavailable endpoint requires a newly authorized endpoint from the
upper-level orchestrator), dependency -> gateway path mapping, forwarding
table compilation, and a candidate plan ``C+_m`` carrying the executable
actions and the next version ``v+_m = v_m + 1``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.controller.cspf import (
    CspfRequest,
    CspfSolver,
    TrafficEngineeringLink,
    reserve_path,
)
from src.tcanet.binding import BindingTable
from src.tcanet.spec import (
    Dependency,
    TaskDAG,
    TaskSpecification,
    World,
)

_PATH_SEP = "->"


def dependency_key(dep: Dependency) -> str:
    """Stable dependency id used across paths/forwarding/binding records."""
    return dep.dep_id


@dataclass(frozen=True)
class PathRecord:
    """``pi_{m,e} = (g_0, ..., g_{H_e})`` for one dependency."""

    dep_id: str
    gateway_path: tuple[str, ...]
    link_ids: tuple[str, ...] = ()

    @property
    def path_id(self) -> str:
        return _PATH_SEP.join(self.gateway_path)


@dataclass(frozen=True)
class ForwardingEntry:
    """One per-task forwarding-table entry in ``FT^g_m``."""

    gateway_id: str
    dep_id: str
    hop_index: int
    src_agent: str
    dst_agent: str
    action_mode: str  # local_delivery | forward_to_gateway
    next_hop_gateway: str | None = None
    rule_id: str = ""

    def __post_init__(self) -> None:
        if not self.rule_id:
            object.__setattr__(
                self,
                "rule_id",
                f"{self.dep_id}:{self.gateway_id}:{self.hop_index}",
            )


@dataclass(frozen=True)
class ExecutableAction:
    """One executable action of the plan ``C+_m`` (paper Sec. IV-A).

    Specifies executor, target, value and preconditions; device-specific
    APIs are out of scope for TCANet.
    """

    action_id: str
    kind: str  # install_rule | remove_rule | bind_support | layer_operation
    executor: str
    target: str
    value: str
    preconditions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SubnetState:
    """``S_m^(v_m)`` — the accepted subnet state at version ``v_m``."""

    task_id: str
    version: int
    paths: dict[str, PathRecord] = field(default_factory=dict)
    forwarding: dict[str, ForwardingEntry] = field(default_factory=dict)
    bindings: BindingTable = field(default_factory=BindingTable)
    accepted: bool = True

    def next_version(self) -> int:
        return self.version + 1


def compile_forwarding(
    dag: TaskDAG,
    paths: dict[str, PathRecord],
) -> dict[str, ForwardingEntry]:
    """Compile per-task gateway forwarding tables from selected paths.

    Intermediate gateways forward the flow along the path; the destination
    gateway delivers it locally; co-located endpoints use local delivery
    (paper Sec. IV-A).
    """
    deps = {dep.dep_id: dep for dep in dag.dependencies}
    entries: dict[str, ForwardingEntry] = {}
    for dep_id in sorted(paths):
        dep = deps[dep_id]
        record = paths[dep_id]
        path = record.gateway_path
        if len(path) == 1:
            entry = ForwardingEntry(
                gateway_id=path[0],
                dep_id=dep_id,
                hop_index=0,
                src_agent=dep.source,
                dst_agent=dep.target,
                action_mode="local_delivery",
            )
            entries[entry.rule_id] = entry
            continue
        for hop_index, gateway_id in enumerate(path):
            last = hop_index == len(path) - 1
            entry = ForwardingEntry(
                gateway_id=gateway_id,
                dep_id=dep_id,
                hop_index=hop_index,
                src_agent=dep.source,
                dst_agent=dep.target,
                action_mode="local_delivery" if last else "forward_to_gateway",
                next_hop_gateway=None if last else path[hop_index + 1],
            )
            entries[entry.rule_id] = entry
    return entries


def solve_dependency_paths(
    task: TaskSpecification,
    world: World,
    *,
    exclude_link_ids: frozenset[str] = frozenset(),
    exclude_gateways: frozenset[str] = frozenset(),
) -> tuple[dict[str, PathRecord], dict[str, str]]:
    """Map each dependency to a gateway path with CSPF (paper Sec. IV-A).

    Returns the per-dependency path records and a failure map; a dependency
    with no feasible path makes the whole construction infeasible.  Link
    capacity is reserved sequentially so shared resources are respected
    during construction.
    """
    solver = CspfSolver()
    te_links = tuple(
        TrafficEngineeringLink(
            link_id=link.link_id,
            source=link.source_gateway,
            target=link.target_gateway,
            available_bandwidth_mbps=(
                world.resources[rid].available_mbps
                if (rid := world.link_resource_id(link.link_id)) in world.resources
                else link.capacity_mbps
            ),
            delay_ms=link.delay_ms,
            te_cost=link.delay_ms,
            up=link.up,
        )
        for link in world.graph.links
        if link.link_id not in exclude_link_ids
        and link.source_gateway not in exclude_gateways
        and link.target_gateway not in exclude_gateways
        and link.up
    )
    paths: dict[str, PathRecord] = {}
    failures: dict[str, str] = {}
    for dep in sorted(task.dag.dependencies, key=dependency_key):
        source = world.endpoints[dep.source]
        target = world.endpoints[dep.target]
        requirements = task.requirements_for(dep)
        request = CspfRequest(
            flow_id=dep.dep_id,
            source=source.gateway_id,
            destination=target.gateway_id,
            required_bandwidth_mbps=dep.demand_mbps,
            maximum_delay_ms=requirements.max_delay_ms,
        )
        result = solver.solve(te_links, request)
        if not result.feasible:
            failures[dep.dep_id] = result.failure_reason
            continue
        paths[dep.dep_id] = PathRecord(
            dep_id=dep.dep_id,
            gateway_path=result.path,
            link_ids=result.link_ids,
        )
        te_links = reserve_path(te_links, result, dep.demand_mbps)
    return paths, failures


@dataclass(frozen=True)
class ConstructionPlan:
    """``C+_m`` — selected paths/tables/bindings plus executable actions."""

    task_id: str
    next_version: int
    paths: dict[str, PathRecord]
    forwarding: dict[str, ForwardingEntry]
    bindings: BindingTable
    actions: tuple[ExecutableAction, ...]
    notes: tuple[str, ...] = ()


class TaskSubnetBuilder:
    """Controller-side construction of the candidate plan (paper Sec. IV-A)."""

    def __init__(self, endpoint_resolver=None) -> None:
        # Replacing an unavailable endpoint requires a newly authorized
        # endpoint from the upper-level orchestrator.
        self._endpoint_resolver = endpoint_resolver

    async def build(
        self,
        task: TaskSpecification,
        world: World,
        bindings: BindingTable,
        current: SubnetState | None = None,
    ) -> ConstructionPlan:
        notes: list[str] = []
        for endpoint in task.dag.endpoints:
            live = world.endpoints.get(endpoint.agent_id)
            if live is not None and live.online:
                continue
            if self._endpoint_resolver is None:
                raise ValueError(
                    f"endpoint {endpoint.agent_id} unavailable and no authorized "
                    "replacement was provided by the orchestrator"
                )
            replacement_gateway = self._endpoint_resolver(endpoint.agent_id, world)
            if replacement_gateway is None:
                raise ValueError(
                    f"orchestrator provided no authorized replacement for "
                    f"endpoint {endpoint.agent_id}"
                )
            world.endpoints[endpoint.agent_id] = _replace_endpoint(
                live or endpoint, replacement_gateway
            )
            notes.append(
                f"endpoint {endpoint.agent_id} replaced by authorized endpoint "
                f"@{replacement_gateway}"
            )

        paths, failures = solve_dependency_paths(task, world)
        if failures:
            raise ValueError(
                "no feasible gateway path for: "
                + ", ".join(f"{dep}:{reason}" for dep, reason in sorted(failures.items()))
            )
        forwarding = compile_forwarding(task.dag, paths)
        version = current.next_version() if current else 1
        actions: list[ExecutableAction] = []
        for rule_id in sorted(forwarding):
            entry = forwarding[rule_id]
            actions.append(
                ExecutableAction(
                    action_id=f"install:{rule_id}",
                    kind="install_rule",
                    executor=entry.gateway_id,
                    target=rule_id,
                    value=entry.action_mode,
                    preconditions={"gateway_online": entry.gateway_id},
                )
            )
        for dep_id in sorted(paths):
            binding = bindings.binding(dep_id)
            for role, agent_id in (
                ("t", binding.t_agent_id),
                ("n", binding.n_agent_id),
                ("p", binding.p_agent_id),
            ):
                if agent_id:
                    actions.append(
                        ExecutableAction(
                            action_id=f"bind:{dep_id}:{role}",
                            kind="bind_support",
                            executor=agent_id,
                            target=dep_id,
                            value=role,
                            preconditions={"agent_online": agent_id},
                        )
                    )
        return ConstructionPlan(
            task_id=task.dag.task_id,
            next_version=version,
            paths=paths,
            forwarding=forwarding,
            bindings=bindings,
            actions=tuple(actions),
            notes=tuple(notes),
        )


def _replace_endpoint(endpoint, gateway_id: str):
    from src.tcanet.spec import Endpoint

    return Endpoint(
        agent_id=endpoint.agent_id,
        name=endpoint.name,
        gateway_id=gateway_id,
        online=True,
    )


def entries_by_gateway(
    forwarding: dict[str, ForwardingEntry],
) -> dict[str, list[ForwardingEntry]]:
    grouped: dict[str, list[ForwardingEntry]] = {}
    for entry in forwarding.values():
        grouped.setdefault(entry.gateway_id, []).append(entry)
    return grouped
